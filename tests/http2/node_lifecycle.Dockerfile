FROM node:24-bookworm-slim
WORKDIR /test
COPY tests/http2/node_lifecycle.mjs ./lifecycle.mjs
CMD ["node", "lifecycle.mjs"]
