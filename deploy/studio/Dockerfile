# 庄严宝刹设计工作台共享服务容器镜像定义
FROM python:3.13-slim

# 设置基础运行环境变量与离线推理约束
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    STUDIO_SAM2_MODEL_DIR=/opt/studio-models/sam2.1-hiera-tiny

# 创建非特权运行账户与持久化目录
RUN groupadd -g 10001 studio && \
    useradd -u 10001 -g 10001 -d /app -s /usr/sbin/nologin -M studio && \
    mkdir -p /app/deploy/studio /data /librechat/images /kb /opt/studio-models/sam2.1-hiera-tiny && \
    chown -R 10001:10001 /app /data && \
    chmod 0555 /kb /opt/studio-models

WORKDIR /app

# 仅使用预编译轮子安装固定版本依赖（含PyTorch CPU与transformers），不保留构建工具
COPY deploy/studio/requirements.txt /app/deploy/studio/requirements.txt
RUN pip install --no-cache-dir --only-binary=:all: -r /app/deploy/studio/requirements.txt

# 在镜像构建阶段拉取固定revision的facebook/sam2.1-hiera-tiny safetensors模型文件并逐一校验SHA256
RUN python -c "import hashlib, os, pathlib, urllib.request; \
repo = 'facebook/sam2.1-hiera-tiny'; \
rev = 'de431c4043854a71d8101e17995dfe596bf101a5'; \
dest = pathlib.Path('/opt/studio-models/sam2.1-hiera-tiny'); \
dest.mkdir(parents=True, exist_ok=True); \
files = { \
    'model.safetensors': '48c14467e5cf9e51870511feb72c89688e82dd74523142c0538b663e193ac2a7', \
    'config.json': 'bfdc695acbd97dc36ff8c2c525faaf25ccdbff2b9c3176fe1323acfa5fb9cf11', \
    'preprocessor_config.json': '2de8ffca4ebddc7fb3efeefcc86fe9327a272a76524e6bbab42f59fa32f55e9c', \
    'processor_config.json': 'f1aa5beebdb6e95b5924f5eb1c0fa007ba3e0a7e4a6d491c4e98b16a3d5a5828', \
}; \
[ \
    ( \
        p := dest / name, \
        urllib.request.urlretrieve(f'https://huggingface.co/{repo}/resolve/{rev}/{name}', p), \
        actual := hashlib.sha256(p.read_bytes()).hexdigest(), \
        (_ for _ in ()).throw( RuntimeError(f'SHA256 mismatch for {name}: {actual} != {expected}') ) if actual != expected else None, \
        os.chmod(p, 0o444), \
    ) \
    for name, expected in files.items() \
]; \
os.chmod(dest, 0o555)"

# 复制只读知识库记录、衍生图与分类目录至 /kb
COPY --chmod=0555 knowledge/records /kb/records
COPY --chmod=0555 knowledge/assets/ref2048 /kb/assets/ref2048
COPY --chmod=0555 knowledge/assets/thumb512 /kb/assets/thumb512
COPY --chmod=0555 knowledge/catalog /kb/catalog

# 复制工作台服务源码与规则数据文件
COPY --chown=10001:10001 deploy/studio /app/deploy/studio

USER 10001:10001
EXPOSE 8000

CMD ["uvicorn", "deploy.studio.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
