FROM node:24.16.0-bookworm-slim AS gateway
WORKDIR /opt/gateway
RUN npm install --omit=dev --no-audit --no-fund supergateway@4.1.0
ADD --checksum=sha256:cf71934546af31cf5d2c6cfe7e56fdb976cbc6c2a8559f451ea8d241edf99efb https://codeload.github.com/blwfish/mcp-agent-notes/tar.gz/dd83adf8bb97755f91dbfcf857dbaeb987883f8f /opt/notes.tgz
RUN mkdir /opt/notes && tar -xzf /opt/notes.tgz -C /opt/notes --strip-components=1

FROM ghcr.io/blwfish/freecad-mcp@sha256:4ffb379ea2717e41eed571b9bd858b3d52cc4a5ec1d941ec23af529388457bdd
COPY --from=gateway /usr/local/bin/node /usr/local/bin/node
COPY --from=gateway /opt/gateway /opt/gateway
COPY --from=gateway /opt/notes /opt/mcp-agent-notes
ADD --chmod=644 --checksum=sha256:4a999d7f9559e4c313415a89c97bf5e5d121a20d1c65ba790b8b08047ddbdc55 https://raw.githubusercontent.com/blwfish/freecad-mcp/dbca328316c6bfd03dba205c9e5f695a7ea5b2f9/usage_guidance.py /opt/freecad-mcp/usage_guidance.py
ENV PYTHONPATH=/opt/mcp-agent-notes/src:/opt/freecad-mcp
# FreeCAD 通过系统账户查询主目录，数值 UID 本身不足以完成初始化。
RUN groupadd --gid 10001 cad && useradd --uid 10001 --gid 10001 --create-home cad
ENV HOME=/home/cad
# 原镜像遗漏了桥接依赖；每个 MCP 会话复用容器入口维护的同一个 CAD 进程。
RUN sed -i '/^"${FREECAD_MCP_FREECAD_BIN}" /d; /^FC_PID=/d' /usr/local/bin/docker-entrypoint.sh
COPY deploy/cad_check.py /opt/freecad-mcp/cad_check.py
COPY deploy/cad_check.py /opt/freecad/Ext/cad_check.py
RUN printf '\nimport cad_check\nNOTES = cad_check.extend_notes(NOTES)\n' >> /opt/freecad-mcp/usage_guidance.py
COPY deploy/freecad.sh /opt/freecad-mcp/start.sh
USER 10001:10001
ENTRYPOINT ["bash", "/opt/freecad-mcp/start.sh"]
CMD ["--stdio", "docker-entrypoint.sh", "--outputTransport", "streamableHttp", "--stateful", "--port", "8000", "--healthEndpoint", "/health"]
