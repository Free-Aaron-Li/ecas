-- ============================================================================
-- 003_migrate_to_minimal.sql —— 一次性迁移留档
--   旧结构：device 台账 + raw_reading（id/device_id/channel/ts/raw/payload/topic/qos/source/created_at）
--           + 视图 v_reading（在库里算 mA/浓度/状态）
--   新结构：只有 raw_reading(model, ts, raw) —— 先生 2026-09-26 定案「只存型号/时间/原始值，
--           其余后端处理」
--
-- ⚠️ 执行前**必须先留底**（旧表里的 payload 是唯一能复现原始报文的凭据）：
--   pg_dump -h 127.0.0.1 -U ecas -d ecas -t device -t raw_reading --data-only \
--     | gzip > /root/ecas/archive/raw_reading_v1-含payload-$(date +%Y%m%d-%H%M%S).sql.gz
--   留底文件同时保存在本机：docs/ 与 scripts/485/archive/（不随库删除而丢失）
--
-- 执行：psql -h 127.0.0.1 -U ecas -d ecas -v ON_ERROR_STOP=1 -f 003_migrate_to_minimal.sql
-- ============================================================================

BEGIN;

-- ① 旧数据先搬进临时表（型号：当时唯一在册设备 dpro66-01 即 DPRO-66）
CREATE TEMP TABLE _old_raw ON COMMIT DROP AS
SELECT ts, raw FROM raw_reading WHERE device_id = 'dpro66-01';

-- ② 拆旧（CASCADE 顺带带走唯一索引与外键；视图先删，否则依赖残留）
DROP VIEW  IF EXISTS v_reading;
DROP TABLE IF EXISTS raw_reading CASCADE;
DROP TABLE IF EXISTS device      CASCADE;

-- ③ 建新（与 001_init.sql 完全一致）
CREATE TABLE raw_reading (
    model text        NOT NULL,
    ts    timestamptz NOT NULL,
    raw   integer     NOT NULL CHECK (raw BETWEEN 0 AND 65535),
    PRIMARY KEY (model, ts, raw)
);
CREATE INDEX raw_reading_model_ts_idx ON raw_reading (model, ts DESC);
COMMENT ON TABLE  raw_reading       IS 'MQTT 上报的原始值：只存 型号/时间/原始值，换算与判定在后端';
COMMENT ON COLUMN raw_reading.model IS '设备型号（不是设备编号），如 DPRO-66';
COMMENT ON COLUMN raw_reading.ts    IS '数据时间：服务器收到该帧的时刻（Asia/Shanghai）';
COMMENT ON COLUMN raw_reading.raw   IS '数据原始值：raw/1000 = mA（12935 → 12.935 mA）';

-- ④ 回填（同一型号同一时刻同一值只留一条）
INSERT INTO raw_reading (model, ts, raw)
SELECT 'DPRO-66', ts, raw FROM _old_raw
ON CONFLICT DO NOTHING;

COMMIT;

-- ⑤ 迁移结果
SELECT count(*) AS rows_after, min(ts) AS first_ts, max(ts) AS last_ts FROM raw_reading;
