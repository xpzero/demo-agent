.DEFAULT_GOAL := help

.PHONY: help init init-env init-backend init-frontend dev dev-backend dev-frontend db-migrate test-backend

help:
	@printf '%s\n' 'make init  初始化后端 uv 环境与前端 pnpm 依赖'
	@printf '%s\n' 'make dev   同时启动本地后端与前端（Ctrl+C 一并停止）'
	@printf '%s\n' 'make db-migrate   执行 PostgreSQL schema migration'
	@printf '%s\n' 'make test-backend 运行后端测试并检查 uv 锁文件'

init: init-env init-backend init-frontend
	@printf '%s\n' '初始化完成。请填写 server/.env，运行 make db-migrate 后启动服务。'

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
	@set -e; backend_pid=''; frontend_pid=''; \
	if curl --silent --output /dev/null --max-time 1 http://127.0.0.1:8000/api/sessions; then \
		printf '%s\n' '后端端口 8000 已有服务，请先停止重复实例'; \
		exit 1; \
	fi; \
	if curl --silent --output /dev/null --max-time 1 http://127.0.0.1:5173/; then \
		printf '%s\n' '前端端口 5173 已有服务，请先停止重复实例'; \
		exit 1; \
	fi; \
	cleanup() { \
		[ -z "$$backend_pid" ] || kill "$$backend_pid" 2>/dev/null || true; \
		[ -z "$$frontend_pid" ] || kill "$$frontend_pid" 2>/dev/null || true; \
		[ -z "$$backend_pid" ] || wait "$$backend_pid" 2>/dev/null || true; \
		[ -z "$$frontend_pid" ] || wait "$$frontend_pid" 2>/dev/null || true; \
	}; \
	trap 'cleanup' EXIT; \
	trap 'exit 130' INT; \
	trap 'exit 143' TERM; \
	(cd server && exec .venv/bin/uvicorn api:app --reload --host 127.0.0.1 --port 8000) & backend_pid=$$!; \
	attempt=0; \
	until curl --fail --silent --show-error http://127.0.0.1:8000/api/sessions >/dev/null 2>&1; do \
		if ! kill -0 "$$backend_pid" 2>/dev/null; then \
			wait "$$backend_pid"; \
			exit $$?; \
		fi; \
		attempt=$$((attempt + 1)); \
		if [ "$$attempt" -ge 50 ]; then \
			printf '%s\n' '后端启动超时，未启动前端'; \
			exit 1; \
		fi; \
		sleep 0.2; \
	done; \
	(cd web && exec ./node_modules/.bin/vite) & frontend_pid=$$!; \
	while kill -0 "$$backend_pid" 2>/dev/null && kill -0 "$$frontend_pid" 2>/dev/null; do \
		sleep 0.2; \
	done; \
	exit_status=0; \
	if ! kill -0 "$$backend_pid" 2>/dev/null; then \
		wait "$$backend_pid" || exit_status=$$?; \
	else \
		wait "$$frontend_pid" || exit_status=$$?; \
	fi; \
	exit "$$exit_status"

dev-backend:
	@cd server && uv run uvicorn api:app --reload --host 127.0.0.1 --port 8000

dev-frontend:
	@cd web && pnpm dev

db-migrate:
	@cd server && uv run python migrate.py

test-backend:
	@cd server && uv run python -m unittest discover -s tests -v
	@cd server && uv lock --check
