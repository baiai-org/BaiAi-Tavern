"""TTS 表现力调教（内置 SKILL）：让主模型按百炼官方规范给语音加「表现力」。

支持两套官方机制（文档：help.aliyun.com/zh/model-studio/non-realtime-tts-user-guide
「指令控制」「情感与富语言标签」两节，均已用真实 Key 实测）：

一、自然语言指令（Qwen3-TTS-Instruct-Flash 系列，非实时）
    ``input.instructions``（≤1600 Token，仅中英文）+ ``input.optimize_instructions``。
    官方「如何编写高质量的声音描述」规范：具体而非模糊 / 多维而非单一 / 客观而非主观 /
    原创而非模仿 / 简洁而非冗余；维度覆盖语速、音调、情感、语气节奏、使用场景。

二、情感与富语言标签（Qwen-Audio-TTS 系列：qwen-audio-3.1-tts-flash /
    3.0-tts-plus / 3.0-tts-flash，非实时）
    直接在待合成文本（``input.text``）里嵌入官方标签：23 个控制类标签
    （``[whispers]`` 耳语、``[angry]`` 愤怒、``[empathetic]`` 共情……作用于其后文本，
    句内可切换情感）+ 7 个富语言标签（``[giggles]`` 咯咯笑、``[sighing]`` 叹息……
    在当前位置插入拟声效果）。

本模块就是「内置 SKILL」：把**待合成文本 + 角色人设 + 当前语境**交给主模型，
按对应模型的官方机制产出调教结果（指令文本 / 带标签文本），再发给 TTS 引擎。
音色本身由 voice 参数固定，调教只改「怎么读」，不改「谁的声音」。

降级原则：主模型未配置 / 调用失败 / 解析为空，都返回空字符串，合成照常进行
（不带指令 / 不带标签），不让调教能力拖垮语音回复。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List, Optional

from common.logging_setup import get_logger

log = get_logger("bot.media.instruct")

#: 指令上限（官方 1600 Token 很宽裕；我们自己收着点，控制成本与效果）
MAX_INSTRUCTION_CHARS = 300

#: 官方示例（SKILL 提示词里教主模型「官方格式长什么样」）
_OFFICIAL_EXAMPLES = (
    "吐字清晰精准，字正腔圆，标准播音风格",
    "年轻活泼的女性声音，语速较快，带有明显的上扬语调，适合介绍时尚产品",
    "沉稳的中年男性，语速缓慢，音色低沉有磁性，适合朗读新闻或纪录片解说",
    "温柔知性的女性，语调平和，适合有声书朗读",
    "可爱的儿童声音，说话略带稚气，适合动画角色配音",
)


def is_tts_instruct_model(model: str) -> bool:
    """模型是否走「指令风格」：仅 Qwen3-TTS-Instruct-Flash 系列（非实时）。

    实时系列（``*-realtime``，WebSocket 协议）、普通 ``qwen3-tts-flash``、
    CosyVoice / Qwen-Audio-TTS 都不在此列。
    """
    m = (model or "").lower()
    return "instruct" in m and "realtime" not in m


def build_tts_instruction_messages(
    text: str,
    character: Optional[Dict[str, Any]] = None,
    context_hint: str = "",
) -> List[Dict[str, str]]:
    """构造「文本 → 官方格式指令」的对话消息（system 教格式，user 给料）。"""
    character = character or {}
    name = str(character.get("name") or "").strip()
    persona = (
        str(character.get("description") or "")
        + "\n"
        + str(character.get("personality") or "")
    ).strip()
    system = (
        "你是 Qwen3-TTS-Instruct（阿里云百炼）的语音导演。"
        "任务：根据角色人设和这句话的语境，写一句自然语言「语音指令」，控制这句话该怎么读出来。\n"
        "【官方编写规范（必须遵守）】\n"
        "1. 具体而非模糊：用具体词描述声音特质（语速偏慢、语调上扬、语气轻快），"
        "避免「好听」「普通」这类模糊词。\n"
        "2. 多维而非单一：组合 2~4 个维度——"
        "语速（快/中/慢/偏快/偏慢）、音调（偏高/偏低/上扬/下沉）、"
        "情感（温柔/俏皮/认真/兴奋/委屈/安心/傲娇/调侃/心疼…）、"
        "语气节奏（停顿、强调、撒娇感、欲言又止）、使用场景（日常聊天/贴心叮嘱/撒娇/安慰…）。\n"
        "3. 客观而非主观：描述可感知的声音特征，不写「我最喜欢的声音」。\n"
        "4. 原创而非模仿：不要求模仿某个具体名人或影视角色。\n"
        "5. 简洁而非冗余：一句话，不堆同义词。\n"
        "官方格式示例：\n"
        + "\n".join("- %s" % e for e in _OFFICIAL_EXAMPLES)
        + "\n【约束】\n"
        "- 音色（谁的声音）已由系统固定，指令里**不要**指定性别、年龄、声线类型"
        "（不写「男声/女声/少年/少女/萝莉/御姐」），只描述「怎么读」。\n"
        "- 情感必须贴合这句话的内容与语境：安慰就温柔放缓，调侃就轻快俏皮，"
        "提问用上扬语调，兴奋就快而扬，难过就低沉放慢，撒娇就软糯拖一点尾音。\n"
        "- 只输出指令本身：中文，60 字以内，一句话；不要引号、不要序号、"
        "不要解释、不要复述这句话。"
    )
    user_parts: List[str] = []
    if name:
        user_parts.append("角色：%s" % name)
    if persona:
        user_parts.append("角色人设：%s" % persona[:400])
    if context_hint:
        user_parts.append("当前语境：%s" % context_hint[:200])
    user_parts.append("要合成的这句话：%s" % text[:300])
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(user_parts)},
    ]


#: 主模型输出里「指令正文开始」的标记（「指令：」/「指令是」/「语音指令为」…）
_MARKER_RE = re.compile(r"指令\s*(?:是|为|如下|就是)?\s*[:：]\s*")
_QUOTE_PAIRS = (
    ("\"", "\""),
    ("“", "”"),
    ("「", "」"),
    ("『", "』"),
    ("‘", "’"),
)


def parse_tts_instruction(raw: str) -> str:
    """清洗主模型输出：围栏 / 前导语 / 引号 / 序号，取指令正文，截断上限。

    覆盖常见输出形态：
    * 纯指令（可能带代码围栏 / 引号包裹）
    * 「指令：xxx」「指令是 xxx」「好的，指令是："xxx"」
    * 多行（首行短标签「以下是指令：」+ 正文行）
    """
    text = (raw or "").strip()
    if not text:
        return ""
    # 代码围栏
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    # 「指令(是/为)：」标记后才是正文，前面是废话
    marker = _MARKER_RE.search(text)
    if marker:
        text = text[marker.end():]
    # 整段被引号包裹时取引号内容；正文中间的强调引号（「重点」之类）保留
    for open_q, close_q in _QUOTE_PAIRS:
        if text.startswith(open_q):
            j = text.rfind(close_q)
            if j >= len(text) - 1:
                inner = text[len(open_q) : j].strip()
                if inner:
                    text = inner
                break
    # 残留的前缀说明（无冒号的情况：「建议指令 温柔一点」）
    changed = True
    while changed:
        changed = False
        new = re.sub(
            r"^(?:(?:语音|建议|推荐)?指令|指令内容|描述|输出|参考)\s*(?:是|为|如下|就是)?\s*[:：]?\s*",
            "",
            text,
        ).strip()
        if new and new != text:
            text = new
            changed = True
    # 残留的悬空引号（只处理不平衡的情况；正文首尾成对的强调引号保留）
    for open_q, close_q in _QUOTE_PAIRS:
        if text.endswith(close_q) and text.count(close_q) > text.count(open_q):
            text = text[: -len(close_q)].rstrip()
            break
        if text.startswith(open_q) and text.count(open_q) > text.count(close_q):
            text = text[len(open_q):].lstrip()
            break
    # 多行输出：取正文行（首行短标签「以下是指令：」之类的丢弃）
    lines = [line.strip() for line in re.split(r"[\n\r]+", text) if line.strip()]
    if len(lines) > 1 and len(lines[0]) <= 12 and lines[0].endswith(("：", ":")):
        lines = lines[1:]
    text = lines[0] if lines else ""
    text = re.sub(r"^\s*(?:-|•|\d+[.、)]\s*)", "", text).strip()
    if len(text) > MAX_INSTRUCTION_CHARS:
        text = text[:MAX_INSTRUCTION_CHARS].rstrip("，、；; ")
    return text


async def generate_tts_instruction(
    llm: Any,
    text: str,
    character: Optional[Dict[str, Any]] = None,
    context_hint: str = "",
    timeout: float = 30.0,
) -> str:
    """用主模型生成官方格式指令；任何失败都返回空串（合成照常，不带指令）。

    思考类模型不限制思考长度（``max_tokens`` 给 4096 上限而非 512——
    预算太小会被 reasoning 吃光导致 content 为空、finish_reason=length；
    4096 只是上限，模型正常停止不额外计费）。仍可能偶发空内容，外层再补一次重试。
    """
    if llm is None:
        return ""
    text = (text or "").strip()
    if not text:
        return ""
    try:
        if not llm.configured():
            return ""
    except Exception:
        return ""
    messages = build_tts_instruction_messages(text, character, context_hint)
    for attempt in range(2):
        try:
            raw = await asyncio.wait_for(
                llm.chat(messages, max_tokens=4096, temperature=0.4),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            log.warning("TTS 指令生成超时（%.0fs），本次合成不带指令", timeout)
            return ""
        except Exception as exc:
            log.warning("TTS 指令生成失败（合成照常）：%s", exc)
            return ""
        instruction = parse_tts_instruction(raw)
        if instruction:
            log.debug("TTS 指令：%s", instruction)
            return instruction
        if attempt == 0:
            log.warning("TTS 指令生成为空（思考类模型偶发），重试一次")
    return ""


# ---------------------------------------------------------------------------
# Qwen-Audio-TTS：情感与富语言标签（嵌入 text）
# ---------------------------------------------------------------------------

#: 官方控制类标签（作用于其后文本，直到下一个控制标签或长句自动切分）
QWEN_AUDIO_CONTROL_TAGS: Dict[str, str] = {
    "sad": "悲伤",
    "amazed": "惊叹",
    "deep and loud shouting": "深沉大声呐喊",
    "trembling": "颤抖",
    "angry": "愤怒",
    "excited": "兴奋",
    "sarcastic": "讽刺",
    "curious": "好奇",
    "like dracula": "德古拉风格（低沉、阴森）",
    "bored": "无聊",
    "tired": "疲惫",
    "scornful": "轻蔑",
    "shouting": "大喊",
    "asmr": "ASMR 轻柔耳语",
    "panicked": "恐慌",
    "mischievously": "调皮",
    "empathetic": "共情",
    "whispers": "耳语",
    "reluctantly": "不情愿",
    "crying": "哭泣",
    "serious": "严肃",
    "very slowly": "非常缓慢地说话",
    "very fast": "非常快速地说话",
}

#: 官方富语言类标签（在当前位置插入一段拟声效果）
QWEN_AUDIO_RICH_TAGS: Dict[str, str] = {
    "gasp": "倒吸一口气",
    "sighing": "叹息",
    "clears throat": "清嗓",
    "giggles": "咯咯笑",
    "laughing": "大笑",
    "cough": "咳嗽",
    "snorts": "哼声、嗤笑",
}

#: 全部合法标签（小写）
_OFFICIAL_TAGS = frozenset(QWEN_AUDIO_CONTROL_TAGS) | frozenset(QWEN_AUDIO_RICH_TAGS)

_TAG_RE = re.compile(r"\[([^\[\]\n]{1,30})\]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

#: 模型常见近似拼写 → 官方标签（防止一个字母之差被当成编造标签删掉）
_TAG_ALIASES = {
    "whisper": "whispers",
    "giggle": "giggles",
    "laugh": "laughing",
    "sigh": "sighing",
    "cry": "crying",
    "snort": "snorts",
    "shout": "shouting",
    "happy": "excited",
    "slow": "very slowly",
    "fast": "very fast",
}


def is_qwen_audio_tts_model(model: str) -> bool:
    """模型是否为 Qwen-Audio-TTS（非实时）：支持情感/富语言标签的系列。

    实时系列（``*-realtime``，WebSocket 协议）与 ASR 模型（``*-asr-*``）不在此列。
    """
    m = (model or "").lower()
    return m.startswith("qwen-audio") and "realtime" not in m and "asr" not in m


def _validate_tags(text: str) -> str:
    """清洗模型输出里的方括号标签，保证只有官方标签会被 TTS 解释：

    * 官方表内（含常见近似拼写归一，不区分大小写）→ 保留并归一成官方写法；
    * 中文方括号（如 [哈哈]）：输出里已出现官方标签时视为模型编造的「中文标签」
      （剥除，否则会被念出来）；完全没有官方标签时视为正文（保留）。
    * 未知英文方括号 → 剥除（如模型编造的 [happy]），并打日志可查。
    """
    tags = [m.group(1).strip().lower() for m in _TAG_RE.finditer(text or "")]
    has_official = any(t in _OFFICIAL_TAGS or _TAG_ALIASES.get(t) in _OFFICIAL_TAGS for t in tags)
    dropped: List[str] = []

    def _repl(match: "re.Match[str]") -> str:
        inner = match.group(1).strip()
        lowered = inner.lower()
        canonical = lowered if lowered in _OFFICIAL_TAGS else _TAG_ALIASES.get(lowered, "")
        if canonical:
            return "[%s]" % canonical
        if _CJK_RE.search(inner) and not has_official:
            return match.group(0)
        dropped.append(inner)
        return ""

    result = _TAG_RE.sub(_repl, text)
    if dropped:
        log.warning("TTS 标签校验剥除了非官方标签：%s", dropped)
    return result


def build_qwen_audio_tag_messages(
    text: str,
    character: Optional[Dict[str, Any]] = None,
    context_hint: str = "",
) -> List[Dict[str, str]]:
    """构造「文本 → 带官方标签文本 + 整体语气指令」的对话消息。"""
    character = character or {}
    name = str(character.get("name") or "").strip()
    persona = (
        str(character.get("description") or "")
        + "\n"
        + str(character.get("personality") or "")
    ).strip()
    control_lines = "\n".join("- [%s] %s" % (k, v) for k, v in QWEN_AUDIO_CONTROL_TAGS.items())
    rich_lines = "\n".join("- [%s] %s" % (k, v) for k, v in QWEN_AUDIO_RICH_TAGS.items())
    system = (
        "你是 Qwen-Audio-TTS（阿里云百炼）的语音导演。"
        "任务：根据角色人设和这句话的语境，对待合成文本做两件事：\n"
        "1. 在文本的合适位置嵌入官方标签（情感/拟声）；\n"
        "2. 写一句整体「怎么读」的指令（控制语速、音调、情绪底色）。\n"
        "【官方语义（必须遵守）】\n"
        "- 控制类标签：设定语音的情感或风格。写在文本中，标签会作用于其后的所有文本，"
        "直到遇到下一个控制类标签，或因句子较长被自动切分为止。\n"
        "- 富语言类标签：在文本的当前位置插入一段拟声效果，不影响前后文本的情感风格。\n"
        "【官方标签表（只能用这些；标签是英文单词，逐字照抄，不许自创、不许用中文标签、"
        "不许改写拼写）】\n"
        "控制类标签：\n" + control_lines + "\n"
        "富语言类标签：\n" + rich_lines + "\n"
        "【官方示例（照这个方式放标签）】\n"
        "[excited]今天的天气真不错！[laughing]我们一起出去玩吧！\n"
        "→ [excited] 是控制类标签，其后文本都带兴奋情感；[laughing] 是富语言标签，"
        "在该位置插入一段笑声后继续合成。\n"
        "[serious]请注意安全事项。[excited]好了，现在让我们开始吧！\n"
        "→ 第一句严肃，第二句起切换为兴奋（同一段文本中可以切换不同情感）。\n"
        "【规则】\n"
        "1. 原文的文字、标点、意思一字不改——只插入标签，不重写、不缩写、不翻译。\n"
        "2. 控制标签必须放在文本最开头（开头情感必须有，不许裸开头）。\n"
        "3. 文本有多个句子时，每句开头都要放控制标签：情感与上一句相同就重复同一个标签"
        "（官方：标签效果会因长句自动切分而中断，逐句放置才能整段覆盖）；"
        "情感变化就换成新标签。逗号中间不放控制标签（句中情感明显转折时才允许换标签）。\n"
        "4. 富语言标签 0~2 个，只有文本本身确实带笑/叹息/倒吸气等情绪时才用，"
        "放在对应词语的位置；它不影响前后文本的情感。\n"
        "5. 精细选标签——贴合角色性格和这句话的内容，从下表选最贴合的那一个：\n"
        "   悲伤难过 [sad]；边哭边说 [crying]；害怕/激动到声音发抖 [trembling]；\n"
        "   愤怒生气 [angry]；讽刺挖苦斗嘴 [sarcastic]；轻蔑不屑嫌弃 [scornful]；\n"
        "   兴奋开心 [excited]；惊叹惊艳 [amazed]；惊讶并倒吸一口气 [gasp]；\n"
        "   好奇追问 [curious]；大声喊话 [shouting]；深沉用力呐喊 [deep and loud shouting]；\n"
        "   慌张六神无主 [panicked]；不情愿不想做 [reluctantly]；无聊没劲 [bored]；\n"
        "   疲惫没精神 [tired]；共情安慰 [empathetic]；压低声音说 [whispers]；\n"
        "   亲密贴耳 ASMR [asmr]；调皮戏谑撒娇 [mischievously]；严肃郑重 [serious]；\n"
        "   语速非常慢 [very slowly]；语速非常快 [very fast]；低沉阴森角色扮演 [like dracula]；\n"
        "   忍俊不禁 [giggles]；放声大笑 [laughing]；叹气认命/无奈 [sighing]；\n"
        "   清清嗓子转换话题 [clears throat]；忍不住哼声嗤笑 [snorts]；咳嗽 [cough]。\n"
        "6. 这句话平铺直叙、没有明显情绪色彩时，选一个轻的中性底色控制标签仍然必须放"
        "（平静叙述用 [serious]，慵懒随意用 [tired]），指令写中性语气。\n"
        "7. 指令一句话、40 字以内，只描述整体语气：语速（快/常速/慢）、音调（偏高/偏低/平）、"
        "情绪底色（关切/轻快/郑重/慵懒…），不要重复标签已表达的情感，不要指定性别年龄。\n"
        "8. 严格输出两行，不要引号、不要序号、不要解释、不要输出原文对照：\n"
        "文本：[标签]文本……\n"
        "指令：怎么读……\n"
        "【你的输出示例】\n"
        "文本：[empathetic]别怕，[empathetic]有我呢。\n"
        "指令：语速偏慢，音调放低，语气温柔关切\n"
        "文本：[very fast]快一点！[very fast]要迟到了！[serious]路上别光顾着看手机。\n"
        "指令：语速偏快，音调稍高，催促中带着关心的叮嘱\n"
        "文本：[giggles][mischievously]哈哈，[mischievously]你居然信了？\n"
        "指令：语速常速，音调轻快，带着调侃的笑意"
    )
    user_parts: List[str] = []
    if name:
        user_parts.append("角色：%s" % name)
    if persona:
        user_parts.append("角色人设：%s" % persona[:400])
    if context_hint:
        user_parts.append("当前语境：%s" % context_hint[:200])
    user_parts.append("要合成的这句话：%s" % text[:300])
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(user_parts)},
    ]


#: 「指令：」行（SKILL v2 要求模型同时输出一句整体语气指令）
_INSTRUCTION_LINE_RE = re.compile(r"^\s*(?:语音)?指令\s*(?:是|为|如下|就是)?\s*[:：]\s*(.*)$")


def _extract_instruction_block(raw: str) -> "tuple[str, str]":
    """从模型输出里拆出「文本 / 指令」两块；没有指令行时指令为空串。

    优先按行找**最后一行**「指令：xxx」（模型可能被诱导把两行输出在一行里，
    找不到行再按行内最后一个「指令：」标记拆）。
    """
    text = raw or ""
    lines = text.splitlines()
    for idx in range(len(lines) - 1, -1, -1):
        match = _INSTRUCTION_LINE_RE.match(lines[idx])
        if match:
            instruction = match.group(1).strip()
            rest = "\n".join(lines[:idx] + lines[idx + 1 :])
            return rest, instruction
    match = re.search(r"指令\s*(?:是|为|如下|就是)?\s*[:：]\s*", text)
    if match and match.end() < len(text):
        head = text[: match.start()]
        tail = text[match.end():]
        newline = tail.find("\n")
        instruction = (tail[:newline] if newline != -1 else tail).strip()
        if instruction and len(instruction) <= 80 and not _TAG_RE.search(instruction):
            return head, instruction
    return text, ""


def parse_tagged_output(raw: str) -> "tuple[str, str]":
    """解析 SKILL v2 输出 → (带标签文本, 整体语气指令)。"""
    text, instruction = _extract_instruction_block(raw)
    tagged = parse_tagged_text(text)
    instruction = parse_tts_instruction(instruction)
    if len(instruction) > 80:
        instruction = instruction[:80].rstrip("，、；; ")
    return tagged, instruction


def parse_tagged_text(raw: str) -> str:
    """清洗主模型输出：围栏 / 前导语 / 引号 / 多行，并校验标签合法性。"""
    text = (raw or "").strip()
    if not text:
        return ""
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    marker = _MARKER_RE.search(text)
    if marker:
        text = text[marker.end():]
    # 短前导语 + 冒号（「好的，改写后的文本是：」/「改写后文本：」/「文本：」）→ 取冒号后
    m = re.match(r"^[^\[\]:：]{1,20}[:：]\s*", text)
    if m:
        text = text[m.end():]
    for open_q, close_q in _QUOTE_PAIRS:
        if text.startswith(open_q):
            j = text.rfind(close_q)
            if j >= len(text) - 1:
                inner = text[len(open_q) : j].strip()
                if inner:
                    text = inner
                break
    changed = True
    while changed:
        changed = False
        new = re.sub(
            r"^(?:改写后?文本|重写后?文本|文本|正文|以下(?:是)?)\s*(?:为|如下|就是)?\s*[:：]?\s*",
            "",
            text,
        ).strip()
        if new and new != text:
            text = new
            changed = True
    for open_q, close_q in _QUOTE_PAIRS:
        if text.endswith(close_q) and text.count(close_q) > text.count(open_q):
            text = text[: -len(close_q)].rstrip()
            break
        if text.startswith(open_q) and text.count(open_q) > text.count(close_q):
            text = text[len(open_q):].lstrip()
            break
    lines = [line.strip() for line in re.split(r"[\n\r]+", text) if line.strip()]
    if len(lines) > 1 and len(lines[0]) <= 12 and lines[0].endswith(("：", ":")):
        lines = lines[1:]
    text = lines[0] if lines else ""
    text = _validate_tags(text)
    return text.strip()


def local_qwen_audio_tags(text: str) -> "tuple[str, str]":
    """本地兜底（主模型不可用时）：按文本线索给最稳的标签 + 中性指令。

    宁可少打标签也不能打错，只覆盖高置信度的线索；无高置信线索时用中性底色
    标签（[serious] 平静叙述）——保证「每次都有效果」的底线由标签+指令双兜底。
    """
    t = (text or "").strip()
    tag = ""
    if re.search(r"(哈哈|嘿嘿|嘻嘻|呵呵)", t):
        tag = "[giggles]"
    elif re.search(r"(呜呜|委屈|难过|心疼|对不起)", t):
        tag = "[sad]"
    elif re.search(r"(快一点|快点|赶紧|来不及|要迟到了|着急)", t) and re.search(r"[!！]", t):
        tag = "[very fast]"
    elif re.search(r"(哼|才不是|讨厌|人家)", t):
        tag = "[mischievously]"
    elif re.search(r"(别怕|没事的|有我|放心吧)", t):
        tag = "[empathetic]"
    elif re.search(r"[\?？]", t) and re.search(r"(吗|呢|什么|为什么|怎么)", t):
        tag = "[curious]"
    if not tag:
        tag = "[serious]"
    instruction = "吐字清晰，语速自然，语气贴合日常对话"
    return ("%s%s" % (tag, t)), instruction


async def generate_qwen_audio_tags(
    llm: Any,
    text: str,
    character: Optional[Dict[str, Any]] = None,
    context_hint: str = "",
    timeout: float = 30.0,
) -> Dict[str, str]:
    """用主模型生成「带官方标签的文本 + 整体语气指令」。

    返回 ``{"text": 带标签文本, "instruction": 指令}``；主模型未配置 / 彻底失败
    时 ``text`` 为空串（调用方用原文 + 本地兜底，保证每次合成都有调教参数）。
    思考类模型不限制思考长度（max_tokens 给 4096 上限，实测思考完整后
    finish=stop 正常输出），空内容最多重试 2 次（共 3 次尝试）。
    """
    empty: Dict[str, str] = {"text": "", "instruction": ""}
    if llm is None:
        return empty
    text = (text or "").strip()
    if not text:
        return empty
    try:
        if not llm.configured():
            return empty
    except Exception:
        return empty
    messages = build_qwen_audio_tag_messages(text, character, context_hint)
    for attempt in range(3):
        try:
            raw = await asyncio.wait_for(
                llm.chat(messages, max_tokens=4096, temperature=0.4),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            log.warning("TTS 情感标签生成超时（%.0fs），本次用本地兜底", timeout)
            return empty
        except Exception as exc:
            log.warning("TTS 情感标签生成失败（用本地兜底）：%s", exc)
            return empty
        tagged, instruction = parse_tagged_output(raw)
        if tagged:
            log.debug("TTS 情感标签文本：%s | 指令：%s", tagged, instruction)
            return {"text": tagged, "instruction": instruction}
        if attempt < 2:
            log.warning("TTS 情感标签生成为空（思考类模型偶发），重试 %d/2", attempt + 1)
    return empty


__all__ = [
    "MAX_INSTRUCTION_CHARS",
    "QWEN_AUDIO_CONTROL_TAGS",
    "QWEN_AUDIO_RICH_TAGS",
    "build_qwen_audio_tag_messages",
    "build_tts_instruction_messages",
    "generate_qwen_audio_tags",
    "generate_tts_instruction",
    "is_qwen_audio_tts_model",
    "is_tts_instruct_model",
    "local_qwen_audio_tags",
    "parse_tagged_output",
    "parse_tagged_text",
    "parse_tts_instruction",
]
