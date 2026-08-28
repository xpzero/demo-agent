.DEFAULT_GOAL := help

.PHONY: help init init-env init-backend init-frontend dev dev-backend dev-frontend

help:
	@printf '%s\n' 'make init  初始化后端 uv 环境与前端 pnpm 依赖'
	@printf '%s\n' 'make dev   同时启动本地后端与前端（Ctrl+C 一并停止）'

init: init-env init-backend init-frontend
	@printf '%s\n' '初始化完成。请填写 server/.env 后启动服务。'

init-env:
	@if [ -f server/.env ]; then \
		printf '%s\n' '保留已有 server/.env'; \
	else \
		cp server/.env.example server/.env; \
		printf '%s\n' '已创建 server/.env，请填写 API 密钥'; \
	fi

init-backend:
	@cd server && uv sync --locked

init-frontend:
	@cd web && pnpm install --frozen-lockfile

dev:
	@set -e; \
	(cd server && uv run uvicorn api:app --reload --host 127.0.0.1 --port 8000) & backend_pid=$$!; \
	(cd web && pnpm dev) & frontend_pid=$$!; \
	trap 'kill "$$backend_pid" "$$frontend_pid" 2>/dev/null || true' EXIT INT TERM; \
	wait "$$backend_pid" "$$frontend_pid"

dev-backend:
	@cd server && uv run uvicorn api:app --reload --host 127.0.0.1 --port 8000

dev-frontend:
	@cd web && pnpm dev
