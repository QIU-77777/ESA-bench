# ESA 评测的任务容器。主容器里跑驱动脚本，评测服务由 compose 的其余服务提供。
FROM node:22-alpine

RUN apk add --no-cache bash curl coreutils python3 py3-pytest

# 用例素材：初始车辆状态与评分标准。
COPY esa/ /opt/esa/

WORKDIR /workspace
