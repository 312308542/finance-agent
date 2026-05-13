-- 全环境统一启用 TimescaleDB，避免开发环境与生产环境行为不一致。
CREATE EXTENSION IF NOT EXISTS timescaledb;

