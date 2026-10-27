import 'package:flutter/material.dart';

import '../core/theme.dart';
import 'widgets.dart';

class SessionTile extends StatelessWidget {
  const SessionTile({
    super.key,
    required this.session,
    this.onTap,
    this.trailing,
    this.highlight = false,
    this.compact = false,
  });

  final Map<String, dynamic> session;
  final VoidCallback? onTap;
  final Widget? trailing;
  final bool highlight;

  /// 紧凑单行：HH:mm + 剧名（省略）+ 座位点 + 状态点；
  /// 不显示房间/DM/价格/门店，冗余信息进详情页。
  final bool compact;

  @override
  Widget build(BuildContext context) {
    if (compact) {
      return _buildCompact(context);
    }
    final rawStatus = textOf(session['status']);
    final status = sessionStatusKey(session['status'], fallback: '待开场');
    final booked = intOf(session['booked_seats']) ?? 0;
    final total = intOf(session['total_seats']);
    final seats = total == null ? '$booked 人（容量待补）' : '$booked / $total 人';
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: Material(
        color: Colors.transparent,
        child: InkWell(
          onTap: onTap,
          borderRadius: BorderRadius.circular(16),
          child: Ink(
            decoration: BoxDecoration(
              color: AppColors.bg2.withValues(alpha: 0.66),
              borderRadius: BorderRadius.circular(16),
              border: Border.all(
                color: highlight
                    ? AppColors.cyan.withValues(alpha: 0.42)
                    : AppColors.line,
              ),
            ),
            child: Padding(
              padding: const EdgeInsets.all(14),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Expanded(
                        child: Text(
                          textOf(session['script_name'], fallback: '未命名剧本'),
                          style: const TextStyle(
                            color: AppColors.text,
                            fontSize: 16,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                      ),
                      if (status == 'cancelled')
                        const Pill(label: '已取消', color: AppColors.danger)
                      else if (status != 'pending')
                        Pill(label: rawStatus, color: AppColors.amber),
                      if (trailing != null) ...[
                        const SizedBox(width: 8),
                        trailing!,
                      ],
                    ],
                  ),
                  const SizedBox(height: 8),
                  Row(
                    children: [
                      const Icon(Icons.schedule,
                          size: 14, color: AppColors.textFaint),
                      const SizedBox(width: 5),
                      Text(
                        '${dateOf(session['start_time'])} ${clockOf(session['start_time'])}',
                        style: const TextStyle(
                          color: AppColors.textDim,
                          fontSize: 12,
                        ),
                      ),
                      const SizedBox(width: 12),
                      const Icon(Icons.storefront_outlined,
                          size: 14, color: AppColors.textFaint),
                      const SizedBox(width: 5),
                      Expanded(
                        child: Text(
                          textOf(session['store_name'], fallback: '未填门店'),
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                            color: AppColors.textDim,
                            fontSize: 12,
                          ),
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 10),
                  Wrap(
                    spacing: 8,
                    runSpacing: 6,
                    children: [
                      Pill(
                        label: seats,
                        color: total != null && booked >= total
                            ? AppColors.amber
                            : AppColors.cyan,
                        icon: Icons.event_seat_outlined,
                      ),
                      Pill(
                        label: textOf(session['room_name'], fallback: '待排房'),
                        color: textOf(session['room_name']).isEmpty
                            ? AppColors.amber
                            : AppColors.mint,
                        icon: Icons.meeting_room_outlined,
                      ),
                      Pill(
                        label: textOf(session['dm_name'], fallback: '待排 DM'),
                        color: textOf(session['dm_name']).isEmpty
                            ? AppColors.amber
                            : AppColors.violet,
                        icon: Icons.person_outline,
                      ),
                      Pill(
                        label: moneyOf(session['list_price']),
                        color: AppColors.textDim,
                        icon: Icons.sell_outlined,
                      ),
                    ],
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }

  Widget _buildCompact(BuildContext context) {
    final rawStatus = textOf(session['status']);
    final status = sessionStatusKey(session['status'], fallback: '待开场');
    final booked = intOf(session['booked_seats']) ?? 0;
    final total = intOf(session['total_seats']);
    // 座位点：满=danger / 紧（余位≤2）=amber / 松=cyan / 容量待补=灰。
    final Color seatColor = total == null
        ? AppColors.textFaint
        : booked >= total
            ? AppColors.danger
            : (total - booked) <= 2
                ? AppColors.amber
                : AppColors.cyan;
    final String seatTip = total == null ? '容量待补' : '$booked / $total 人';
    // 状态点：待开场=灰 / 已开场（及其他）=绿 / 已取消=红。
    final Color statusColor = status == 'cancelled'
        ? AppColors.danger
        : status == 'pending'
            ? AppColors.textFaint
            : AppColors.mint;
    final String statusTip = status == 'cancelled'
        ? '已取消'
        : status == 'pending'
            ? '待开场'
            : rawStatus;
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Material(
        color: Colors.transparent,
        child: InkWell(
          onTap: onTap,
          borderRadius: BorderRadius.circular(12),
          child: Ink(
            decoration: BoxDecoration(
              color: AppColors.bg2.withValues(alpha: 0.66),
              borderRadius: BorderRadius.circular(12),
              border: Border.all(
                color: highlight
                    ? AppColors.cyan.withValues(alpha: 0.42)
                    : AppColors.line,
              ),
            ),
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
              child: Row(
                children: [
                  Text(
                    clockOf(session['start_time']),
                    style: const TextStyle(
                      color: AppColors.cyan,
                      fontSize: 13,
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                  const SizedBox(width: 10),
                  Expanded(
                    child: Text(
                      textOf(session['script_name'], fallback: '未命名剧本'),
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        color: AppColors.text,
                        fontSize: 14,
                      ),
                    ),
                  ),
                  Tooltip(
                    message: seatTip,
                    child: _Dot(color: seatColor),
                  ),
                  const SizedBox(width: 8),
                  Tooltip(
                    message: statusTip,
                    child: _Dot(color: statusColor),
                  ),
                  if (trailing != null) ...[
                    const SizedBox(width: 4),
                    trailing!,
                  ],
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}

class _Dot extends StatelessWidget {
  const _Dot({required this.color});

  final Color color;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: 8,
      height: 8,
      decoration: BoxDecoration(color: color, shape: BoxShape.circle),
    );
  }
}
