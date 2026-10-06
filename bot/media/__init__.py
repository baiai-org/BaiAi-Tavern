"""多媒体收发模块（V0.2 图片 / 语音）。

* :mod:`bot.media.store`    收发的媒体文件落地 / 清理
* :mod:`bot.media.voice`    TTS（文字转语音）
* :mod:`bot.media.images`   图像生成与图像理解
* :mod:`bot.media.hub`      MediaHub：串联附件解析→回复→上传发送

语音转文字不在此模块：QQ 官方平台随语音消息推送参考转写文本，
:mod:`bot.media.hub` 直接使用。
"""
