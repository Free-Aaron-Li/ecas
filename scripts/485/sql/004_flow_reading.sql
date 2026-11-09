-- ============================================================================
-- 004_flow_reading.sql —— 电磁流量计（FMC400）原始寄存器块表（PostgreSQL 16）
--
-- ⭐ 口径延续 001（2026-09-26 先生定案「库里只存原始值」）：
--      流量计一次问 14 个寄存器（0~13：流速/瞬时流量/百分比/空管/正反累积/报警/流向），
--      它不像浓度计只有一个 raw。因此这里存**整块寄存器原样**（integer[14]），
--      单位换算、32 位拼接、状态判定仍在**后端**（`web/backend/app/flow.py`）。
--      ⇒ 不改历史数据即可调整口径；库里没有 flow_m3h 之类的换算结果列。
--
-- 一行 = 一次成功应答（1 帧 14 寄存器）。同一时刻重复上报由 PK 天然去重，
-- 入库端用 `ON CONFLICT (model, ts) DO NOTHING`。
--
-- 幂等：可重复执行（IF NOT EXISTS）
-- 执行：docker exec -i ecas-db psql -U ecas -d ecas -v ON_ERROR_STOP=1 -f /dev/stdin < 004_flow_reading.sql
--       （本机开发：psql -p 55432 -U ecas -d ecas_dev -v ON_ERROR_STOP=1 -f 004_flow_reading.sql）
-- ============================================================================

CREATE TABLE IF NOT EXISTS flow_reading (
    model text        NOT NULL,                      -- 设备型号，固定 'FMC400'
    ts    timestamptz NOT NULL,                      -- 数据时间（服务器发出该次询问/收到应答的时刻）
    regs  integer[]   NOT NULL,                      -- 寄存器 0~13 原样（每个 0..65535）
    CONSTRAINT flow_reading_regs_len CHECK (cardinality(regs) = 14),  -- 只收「读 0~13」的整块
    PRIMARY KEY (model, ts)
);

-- 查询主路径：按型号取最近 N 条 / 按时间段做趋势
CREATE INDEX IF NOT EXISTS flow_reading_model_ts_idx ON flow_reading (model, ts DESC);

COMMENT ON TABLE  flow_reading       IS 'FMC400 原始寄存器块：只存 型号/时间/14 个寄存器，换算与判定在后端';
COMMENT ON COLUMN flow_reading.model IS '设备型号，固定 FMC400（电磁流量计，站号 8）';
COMMENT ON COLUMN flow_reading.ts    IS '数据时间：主机发出该次 MODBUS 询问的时刻（Asia/Shanghai）';
COMMENT ON COLUMN flow_reading.regs  IS '寄存器 0~13 原样：0/1 流速×1000、2/3 瞬时流量×100、4 百分比×100、5 空管×100、6/7 正累积、8/9 反累积、12 报警位、13 流向';

-- ---------------------------------------------------------------- 自检
DO $$
DECLARE n bigint;
BEGIN
    SELECT count(*) INTO n FROM flow_reading;
    RAISE NOTICE '初始化完成：flow_reading % 行；列 = model / ts / regs[14]', n;
END $$;
