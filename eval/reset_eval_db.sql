-- 把评测库重置到可重复跑的状态。
-- 用法：docker exec -i pg-local psql -U postgres -d customer_service_eval < eval/reset_eval_db.sql
--
-- 为什么需要它：退款/取消会真改状态，第二次跑同一订单，工具会因为「订单已退款」而拒绝，
-- 落库断言 expectation(delta=1) 就不成立了。评测本身要可复现，所以跑前必须归零。
-- 删父表前先删子表，否则 refund_audit_logs 的外键会挡住。

BEGIN;

-- 1. 卡单：退款/取消涉及的两个订单回归 pending
UPDATE orders SET status = 'pending', shipped_at = NULL
WHERE order_no IN ('WAORD009', 'WAORD010');

-- 2. 已发货订单（用于"退款应被拒绝"的题）保持 shipped
UPDATE orders SET status = 'shipped'
WHERE order_no = 'WAORD004';

-- 3. 清掉评测过程中产生的退款单（先清审计，再清主表）
DELETE FROM refund_audit_logs
WHERE refund_id IN (
    SELECT id FROM refunds
    WHERE user_id IN (4, 5) AND created_at > now() - interval '6 hours'
);
DELETE FROM refunds
WHERE user_id IN (4, 5) AND created_at > now() - interval '6 hours';

-- 4. 转人工标记与备注归零（escalate_to_human 写这两列）
UPDATE chat_sessions
SET needs_human = false, needs_human_reason = NULL, agent_note = NULL
WHERE user_id IN (4, 5) AND created_at > now() - interval '6 hours';

COMMIT;

-- 自检：这三行应该分别是 pending / pending / shipped，且没有残留退款单
SELECT order_no, status FROM orders WHERE order_no IN ('WAORD009','WAORD010','WAORD004') ORDER BY order_no;
SELECT count(*) AS leftover_refunds FROM refunds
WHERE user_id IN (4, 5) AND created_at > now() - interval '6 hours';
