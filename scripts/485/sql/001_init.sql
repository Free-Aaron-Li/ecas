-- ============================================================================
-- 001_init.sql —— ECAS「原始值库」初始化（PostgreSQL 16）
--
-- ⭐ 核心口径（2026-09-26 先生定案）：
--      库里**只存三样**：设备型号 model ｜ 数据时间 ts ｜ 数据原始值 raw。
--      其余一切（mA、浓度、补偿系数、断线/超量程判定、设备台账、告警规则）
--      全部由**后端**处理，不进数据库。
--      ⇒ 所以本文件**没有** device 表、**没有**视图、**没有** payload/source 等留痕列。
--
-- 幂等：可重复执行（IF NOT EXISTS / ON CONFLICT）
-- 执行：psql -h 127.0.0.1 -U ecas -d ecas -v ON_ERROR_STOP=1 -f 001_init.sql
--       （口令走 /root/.pgpass，不需也不应写在命令行里）
-- ============================================================================

-- ---------------------------------------------------------------- 主表（唯一一张表）
CREATE TABLE IF NOT EXISTS raw_reading (
    model text        NOT NULL,                                    -- 设备型号，如 'DPRO-66'
    ts    timestamptz NOT NULL,                                    -- 数据时间（服务器收到该帧的时刻）
    raw   integer     NOT NULL CHECK (raw BETWEEN 0 AND 65535),    -- 数据原始值（4~20mA 模块的输入寄存器值）
    PRIMARY KEY (model, ts, raw)                                   -- ⭐ 天然去重：DTU 重发、日志重放都不会写两遍
);

-- 查询主路径：按型号取最近 N 条 / 按时间段做趋势（PK 已是 (model,ts,raw)，缺 ts DESC 方向）
CREATE INDEX IF NOT EXISTS raw_reading_model_ts_idx ON raw_reading (model, ts DESC);

COMMENT ON TABLE  raw_reading       IS 'MQTT 上报的原始值：只存 型号/时间/原始值，换算与判定在后端';
COMMENT ON COLUMN raw_reading.model IS '设备型号（不是设备编号），如 DPRO-66';
COMMENT ON COLUMN raw_reading.ts    IS '数据时间：服务器收到该帧的时刻（Asia/Shanghai，payload 本身不带时间戳）';
COMMENT ON COLUMN raw_reading.raw   IS '数据原始值：raw/1000 = mA（12935 → 12.935 mA）';

-- ---------------------------------------------------------------- 自检
DO $$
DECLARE n bigint;
BEGIN
    SELECT count(*) INTO n FROM raw_reading;
    RAISE NOTICE '初始化完成：raw_reading % 行；列 = model / ts / raw', n;
END $$;
