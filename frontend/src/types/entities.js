// API实体字段约定：
// 来源{id,natural_key,data}、口径{id,version,data}、报告{id,status,snapshot,checksum,result,approved}、计划{next_at,enabled}。
// 占用去重（result.occupancy）：{global_minutes,raw_minutes,apportion[],date_totals[],segments[],segment_count,case_count,method,warnings,rounding}。
// 后端配套表 occupancy_checkpoints（检查点/跨段在途单）、occupancy_segments（重叠段，run+ordinal唯一）、
// occupancy_apportion（日期×小组整数分摊，发布闸门守恒后写入）、occupancy_traces（逐笔追溯）。
export const ENTITY_KEYS=['sources','policies','runs','schedules','deliveries'];
