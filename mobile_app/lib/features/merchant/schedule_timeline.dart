/// 排期时间轴纯函数：视窗为 10:00 → 次日 04:00，共 18 小时。
///
/// 跨午夜的凌晨场（00:00–03:59）映射到 +14h 段；05:00–09:59 的
/// 偏移为负，视为视窗外（渲染端 clamp 到边并标记）。可脱离 widget 单测。
library;

/// 视窗起点小时（本地时间）。
const int timelineStartHour = 10;

/// 视窗总长（分钟）：18 小时。
const int timelineSpanMinutes = 18 * 60;

/// 把一天内的时刻映射到时间轴分钟偏移。
///
/// 10:00→0；19:00→540；次日 01:00→900；05:00–09:59 为负（视窗外）。
int timelineOffsetMinutes(DateTime time) {
  final minuteOfDay = time.hour * 60 + time.minute;
  const startMinute = timelineStartHour * 60;
  if (minuteOfDay >= startMinute) {
    return minuteOfDay - startMinute;
  }
  if (time.hour < 5) {
    // 凌晨 00:00–04:59 属于前一天开场的延续段：+14 小时；
    // 04:00 起即视窗末界（≥1080 不可见，渲染端 clamp 到底边并标记）。
    return minuteOfDay + (24 * 60 - startMinute);
  }
  return minuteOfDay - startMinute;
}

/// 偏移是否落在视窗内（用于区分正常块与越界小标记）。
bool timelineVisible(int offsetMinutes) =>
    offsetMinutes >= 0 && offsetMinutes < timelineSpanMinutes;

/// 偏移 → 像素 top；越界 clamp 到视窗边（渲染端另行标记越界）。
double blockTop(int offsetMinutes, {double pxPerMinute = 1.4}) =>
    offsetMinutes.clamp(0, timelineSpanMinutes) * pxPerMinute;

/// 场次块高度用的时长：缺省 45 分钟，夹在 15–720 分钟之间。
int clampDurationMinutes(int? minutes) {
  if (minutes == null || minutes <= 0) {
    return 45;
  }
  return minutes.clamp(15, 720);
}

/// 时间轴小时刻度：10..23 然后次日 0..4；04:00 为视窗底界收边。
const List<int> timelineHourMarks = [
  10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 0, 1, 2, 3, 4,
];

/// 场次块的渲染 top：视窗内（offset < 18h）按真实偏移换算，绝不位移——
/// 相邻深夜场次（如 03:00 与 03:45）不虚构重叠；04:00 之后（offset ≥ 视窗长）
/// 的块贴底 clamp，保持部分可见（越界标记由渲染端展示）。
double timelineBlockTop(
  int offsetMinutes, {
  required double blockHeight,
  double pxPerMinute = 1.4,
}) {
  final raw = blockTop(offsetMinutes, pxPerMinute: pxPerMinute);
  if (offsetMinutes < timelineSpanMinutes) {
    return raw;
  }
  final maxTop = timelineSpanMinutes * pxPerMinute - blockHeight;
  return raw.clamp(0.0, maxTop < 0.0 ? 0.0 : maxTop);
}

String _ymd(DateTime day) => '${day.year.toString().padLeft(4, '0')}-'
    '${day.month.toString().padLeft(2, '0')}-'
    '${day.day.toString().padLeft(2, '0')}';

/// 营业日分组键（'YYYY-MM-DD'）：凌晨 00:00–04:59 归入前一天。
///
/// 与 [timelineOffsetMinutes] 的 +14h 映射一致：10-24 01:00 的场次
/// 属于 10-23 营业日的时间轴（视窗 10-23 10:00 → 10-24 04:00）。
/// 解析失败回退原串；仅时间轴分组使用，紧凑列表仍按自然日。
String timelineDateKey(String raw) {
  final time = DateTime.tryParse(raw);
  // 无时刻部分（纯日期串）没有营业日归属信息，按原串返回；
  // 只有带 HH:mm 的完整时间戳才做凌晨归前一天的判定。
  if (time == null || raw.length <= 10) {
    return raw;
  }
  if (time.hour < 5) {
    return _ymd(time.subtract(const Duration(days: 1)));
  }
  return _ymd(time);
}
