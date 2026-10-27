"""网页版后端。

模块划分：
  config.py    环境变量解析与校验
  security.py  邮箱、哈希、token、CSRF、同源判断
  mailer.py    Resend / 本地控制台 / 测试录制三种邮件发送器
  access.py    访问申请与审批的 SQLite 存储
"""
