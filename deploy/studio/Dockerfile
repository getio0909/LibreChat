# 庄严宝刹设计工作台共享服务容器镜像定义
FROM python:3.13-slim

# 设置基础运行环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# 创建非特权运行账户与持久化目录
RUN groupadd -g 10001 studio && \
    useradd -u 10001 -g 10001 -d /app -s /usr/sbin/nologin -M studio && \
    mkdir -p /app/deploy/studio /data /librechat/images && \
    chown -R 10001:10001 /app /data

WORKDIR /app

# 仅使用预编译轮子安装固定版本依赖，不保留构建工具
COPY deploy/studio/requirements.txt /app/deploy/studio/requirements.txt
RUN pip install --no-cache-dir --only-binary=:all: -r /app/deploy/studio/requirements.txt

# 复制工作台服务源码与规则数据文件
COPY --chown=10001:10001 deploy/studio /app/deploy/studio

USER 10001:10001
EXPOSE 8000

CMD ["uvicorn", "deploy.studio.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
