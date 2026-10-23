"""Display labels; persisted backend outcome codes remain unchanged."""
NORMAL_OUTCOMES = ("consistent","response_observed","parameter_within_limit")
OUTCOME_LABELS = {
    "parameter_above_limit":"参数越限", "parameter_within_limit":"参数恢复正常",
    "consistent":"一致", "inconsistent":"不一致", "unit_mismatch":"单位不一致",
    "non_numeric_pair":"输入不是数值", "numeric_overflow":"数值溢出",
    "response_observed":"已观测到响应", "response_cancelled":"输入中断，取消判定",
    "response_superseded":"被新指令替代", "response_missing_baseline":"缺少响应基线",
    "response_already_high":"反馈原已超过阈值", "response_time_overflow":"响应时间溢出",
    "response_time_ambiguous":"响应时间无法确定", "response_timeout":"响应超时",
    "response_observation_gap":"响应观测存在缺口", "response_unresolved":"结束时响应尚未确定",
}
