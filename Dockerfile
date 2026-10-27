FROM python:3.13-slim

# Hugging Face Spaces 要求容器以内置的 UID 1000 运行；Render 等平台同样接受
# 非 root 用户。保持 UID 固定，避免在 HF 上因权限问题无法写 data/。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

RUN useradd --create-home --uid 1000 user

WORKDIR /home/user/app

# 启动时 ensure_directories() 要往 data/ 里建目录；WORKDIR 建出来的目录属主是
# root，必须先改回 user，否则 UID 1000 运行时直接 PermissionError。
RUN mkdir -p /home/user/app/data && chown -R user:user /home/user

COPY --chown=user:user requirements.txt ./
# 大陆机器直连 pypi.org 经常慢到超时，构建时可以换源：
#   PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple docker compose build
# 默认仍是官方源，境外构建不受影响。
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN python -m pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r requirements.txt

COPY --chown=user:user app ./app

USER user

EXPOSE 8000

# 单 worker 是刻意限制：InMemorySaver 只存在于当前进程，多 worker 会让
# 同一用户的对话记忆分裂。数据库模式支持跨进程写锁，但没有解决记忆分裂；
# Excel 兼容模式也没有跨进程锁。不要在这里加 --workers N。
# --no-access-log：审批/备用链接把 token 放在 URL 里，默认访问日志会把它
# 原样写进平台日志；应用层只记录申请编号和异常类型。
CMD ["sh", "-c", "exec uvicorn app.web.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips='*' --no-access-log"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["sh", "-c", "python -c \"import os, sys, urllib.request; port = os.environ.get('PORT', '8000'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=3).status == 200 else 1)\""]
