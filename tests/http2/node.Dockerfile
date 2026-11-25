FROM node:24-bookworm-slim
WORKDIR /fixture
COPY tests/http2/node_http2.mjs ./node_http2.mjs
CMD ["node", "node_http2.mjs"]
