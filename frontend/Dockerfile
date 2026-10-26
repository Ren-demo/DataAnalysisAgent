# ============================================================
# 前端镜像：Vite 构建 + Node 静态服务（不使用 nginx）
#
# 运行时只依赖 Node 内置模块，server.mjs 同时承担：
#   1) 伺服 dist/ 静态资源
#   2) 把 /api/** 反向代理到后端容器（同源，无 CORS）
#   3) SSE 字节级透传（不缓冲）
# ============================================================

FROM node:22-alpine AS builder

WORKDIR /app

# 先只拷依赖清单，命中 Docker 层缓存：源码改动不会导致重装 node_modules
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
# 与 package.json 的 build 脚本一致：vue-tsc 类型检查 + vite build
RUN npm run build


FROM node:22-alpine AS runtime

ENV NODE_ENV=production \
    PORT=3000 \
    BACKEND_URL=http://backend:8000

WORKDIR /app

COPY --from=builder /app/dist ./dist
COPY server.mjs ./

# node 镜像自带非 root 用户 node（uid 1000）；监听 3000（>1024）无需特权
USER node

EXPOSE 3000

CMD ["node", "server.mjs"]