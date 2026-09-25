-- ============================================================================
-- 002_queries.sql —— 常用查询（给人看 / 给后续网页与后端做参考实现）
--
-- ⭐ 口径（2026-09-26 先生定案）：库里只存 型号/时间/原始值；
--    mA、浓度、补偿、断线/超量程判定全部在**后端**做。
--    本文件里的换算表达式只为「临时人工查库」方便，参数写在各自的 CTE 里，
--    正式实现请放在后端（改补偿不用动历史数据，因为库里根本没有换算结果）。
--
-- 后端需要的参数（原 device 表内容，现已移出数据库，放在后端配置里）：
--   型号 DPRO-66 ｜ i_lo=4.000 i_hi=20.000 ｜ c_lo=0 c_hi=100 ｜ offset_conc=+0.5
--   ｜ break_low=3600（<3.6mA 判断线） ｜ over_high=20500（>20.5mA 判超量程）
--
-- 用法：psql -h 127.0.0.1 -U ecas -d ecas -f 002_queries.sql
-- ============================================================================

-- ① 最近 20 条（现场第一眼：有没有数据、值合不合理；含后端口径的换算预览）
WITH p AS (SELECT 4.000::numeric AS i_lo, 20.000 AS i_hi, 0::numeric AS c_lo,
                  100::numeric AS c_hi, 0.5 AS offset_conc,
                  3600 AS break_low, 20500 AS over_high)
SELECT r.ts, r.raw,
       round(r.raw / 1000.0, 3) AS ma,
       round((r.raw - p.i_lo * 1000) * (p.c_hi - p.c_lo) / ((p.i_hi - p.i_lo) * 1000) + p.c_lo,
             3)                                        AS conc_raw,
       round((r.raw - p.i_lo * 1000) * (p.c_hi - p.c_lo) / ((p.i_hi - p.i_lo) * 1000) + p.c_lo
             + p.offset_conc, 3)                       AS conc,
       CASE WHEN r.raw < p.break_low THEN '断线'
            WHEN r.raw > p.over_high THEN '超量程'
            ELSE '正常' END                            AS status
FROM raw_reading r, p
WHERE r.model = 'DPRO-66'
ORDER BY r.ts DESC
LIMIT 20;

-- ② 最近 24 小时统计（n = 0 说明这一整天没收到数据帧）
SELECT count(*)              AS n,
       min(raw)              AS raw_min,
       max(raw)              AS raw_max,
       round(avg(raw) / 1000.0, 3) AS avg_ma
FROM raw_reading
WHERE model = 'DPRO-66' AND ts > now() - interval '24 hours';

-- ③ 每 10 分钟均值（趋势用；600 可改 60 / 3600）
SELECT to_timestamp(floor(extract(epoch FROM ts) / 600) * 600) AS bucket_start,
       count(*)                                                AS n,
       min(raw)                                                AS raw_min,
       max(raw)                                                AS raw_max,
       round(avg(raw) / 1000.0, 3)                             AS avg_ma
FROM raw_reading
WHERE model = 'DPRO-66' AND ts > now() - interval '7 days'
GROUP BY 1
ORDER BY 1 DESC;

-- ④ 异常段：断线（raw < 3600）/ 超量程（raw > 20500）—— 报警与排查用
SELECT ts, raw, round(raw / 1000.0, 3) AS ma,
       CASE WHEN raw < 3600 THEN '断线' ELSE '超量程' END AS status
FROM raw_reading
WHERE model = 'DPRO-66' AND (raw < 3600 OR raw > 20500)
ORDER BY ts DESC
LIMIT 50;

-- ⑤ 数据缺口检查（相邻两条间隔 > 5 分钟 ⇒ DTU / 链路断过）
SELECT ts AS gap_from, next_ts, round(extract(epoch FROM next_ts - ts) / 60.0, 1) AS gap_min
FROM (SELECT ts, lead(ts) OVER (ORDER BY ts) AS next_ts
      FROM raw_reading WHERE model = 'DPRO-66') x
WHERE next_ts - ts > interval '5 minutes'
ORDER BY ts DESC
LIMIT 50;

-- ⑥ 每天入库量（看采集是否稳定；按理论 100 秒/帧，一天应约 864 条）
SELECT ts::date AS day, count(*) AS n
FROM raw_reading
WHERE model = 'DPRO-66'
GROUP BY 1
ORDER BY 1 DESC;

-- ⑦ 有哪些型号、各多少条、时间跨度
SELECT model, count(*) AS n, min(ts) AS first_ts, max(ts) AS last_ts
FROM raw_reading
GROUP BY model
ORDER BY n DESC;

-- ⑧ 重复时间戳抽查（应为 0 行；唯一键 (model, ts, raw) 已兜底）
SELECT ts, count(*) AS n
FROM raw_reading
GROUP BY model, ts
HAVING count(*) > 1
ORDER BY n DESC
LIMIT 20;
