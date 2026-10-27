import 'package:flutter/material.dart';

import '../core/theme.dart';

Map<String, dynamic> asMap(Object? value) {
  if (value is Map<String, dynamic>) {
    return value;
  }
  if (value is Map) {
    return value.map((key, item) => MapEntry(key.toString(), item));
  }
  return const {};
}

List<Map<String, dynamic>> asMapList(Object? value) {
  if (value is List) {
    return value.map(asMap).toList();
  }
  return const [];
}

List<String> asStringList(Object? value) {
  if (value is List) {
    return value.map((item) => textOf(item)).where((item) => item.isNotEmpty).toList();
  }
  return const [];
}

String textOf(Object? value, {String fallback = ''}) {
  if (value == null) {
    return fallback;
  }
  final text = value.toString().trim();
  return text.isEmpty ? fallback : text;
}

int? intOf(Object? value) {
  if (value is num) {
    return value.toInt();
  }
  return int.tryParse(textOf(value));
}

double? doubleOf(Object? value) {
  if (value is num) {
    return value.toDouble();
  }
  return double.tryParse(textOf(value));
}

String moneyOf(Object? value) {
  final amount = doubleOf(value);
  if (amount == null) {
    return '价格待补';
  }
  if (amount == amount.roundToDouble()) {
    return '¥${amount.toInt()}';
  }
  return '¥${amount.toStringAsFixed(2)}';
}

String clockOf(Object? value) {
  final text = textOf(value);
  if (text.length >= 16) {
    return text.substring(11, 16);
  }
  return text;
}

String dateOf(Object? value) {
  final text = textOf(value);
  if (text.length >= 10) {
    return text.substring(0, 10);
  }
  return text;
}

String durationOf(Object? seconds) {
  final total = (intOf(seconds) ?? 0).clamp(0, 359999);
  final hours = total ~/ 3600;
  final minutes = (total % 3600) ~/ 60;
  final rest = total % 60;
  if (hours > 0) {
    return '${hours.toString().padLeft(2, '0')}:'
        '${minutes.toString().padLeft(2, '0')}:'
        '${rest.toString().padLeft(2, '0')}';
  }
  return '${minutes.toString().padLeft(2, '0')}:'
      '${rest.toString().padLeft(2, '0')}';
}

class Panel extends StatelessWidget {
  const Panel({
    super.key,
    required this.child,
    this.padding = const EdgeInsets.all(16),
    this.borderColor,
    this.color,
  });

  final Widget child;
  final EdgeInsets padding;
  final Color? borderColor;
  final Color? color;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: padding,
      decoration: BoxDecoration(
        color: color ?? AppColors.bg2.withValues(alpha: 0.66),
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: borderColor ?? AppColors.line),
      ),
      child: child,
    );
  }
}

class Pill extends StatelessWidget {
  const Pill({
    super.key,
    required this.label,
    this.color = AppColors.textDim,
    this.icon,
  });

  final String label;
  final Color color;
  final IconData? icon;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 5),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(999),
        border: Border.all(color: color.withValues(alpha: 0.32)),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          if (icon != null) ...[
            Icon(icon, size: 13, color: color),
            const SizedBox(width: 4),
          ],
          Text(
            label,
            style: TextStyle(
              color: color,
              fontSize: 11,
              fontWeight: FontWeight.w600,
            ),
          ),
        ],
      ),
    );
  }
}

class EmptyHint extends StatelessWidget {
  const EmptyHint({
    super.key,
    required this.title,
    this.message,
    this.icon = Icons.inbox_outlined,
  });

  final String title;
  final String? message;
  final IconData icon;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 28, horizontal: 16),
      child: Column(
        children: [
          Icon(icon, color: AppColors.textFaint, size: 30),
          const SizedBox(height: 10),
          Text(
            title,
            style: const TextStyle(
              color: AppColors.textDim,
              fontWeight: FontWeight.w600,
            ),
          ),
          if (message != null) ...[
            const SizedBox(height: 6),
            Text(
              message!,
              textAlign: TextAlign.center,
              style: const TextStyle(
                color: AppColors.textFaint,
                fontSize: 12,
                height: 1.5,
              ),
            ),
          ],
        ],
      ),
    );
  }
}

class InlineError extends StatelessWidget {
  const InlineError({
    super.key,
    required this.message,
    this.onRetry,
  });

  final String message;
  final VoidCallback? onRetry;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: AppColors.danger.withValues(alpha: 0.10),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: AppColors.danger.withValues(alpha: 0.34)),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Icon(Icons.error_outline, color: AppColors.danger, size: 18),
          const SizedBox(width: 10),
          Expanded(
            child: Text(
              message,
              style: const TextStyle(
                color: AppColors.text,
                fontSize: 13,
                height: 1.45,
              ),
            ),
          ),
          if (onRetry != null)
            TextButton(
              onPressed: onRetry,
              child: const Text('重试'),
            ),
        ],
      ),
    );
  }
}

class SectionTitle extends StatelessWidget {
  const SectionTitle({
    super.key,
    required this.title,
    this.trailing,
  });

  final String title;
  final Widget? trailing;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 10, top: 4),
      child: Row(
        children: [
          Expanded(
            child: Text(
              title,
              style: const TextStyle(
                color: AppColors.text,
                fontSize: 15,
                fontWeight: FontWeight.w700,
                letterSpacing: 0.3,
              ),
            ),
          ),
          if (trailing != null) trailing!,
        ],
      ),
    );
  }
}

/// 服务端场次状态是中文（待开场/已开场/已取消），界面比较统一走这个
/// 映射后的 key；未知值原样透传，将来服务端加新状态也不会被误判成旧值。
String sessionStatusKey(Object? raw, {String fallback = ''}) {
  final value = textOf(raw, fallback: fallback);
  switch (value) {
    case '待开场':
      return 'pending';
    case '已开场':
      return 'started';
    case '已取消':
      return 'cancelled';
    default:
      return value;
  }
}

Color runStatusColor(String status) {
  switch (status) {
    case 'active':
      return AppColors.mint;
    case 'paused':
      return AppColors.amber;
    case 'pending':
      return AppColors.cyan;
    case 'completed':
      return AppColors.textDim;
    case 'abandoned':
      return AppColors.danger;
    default:
      return AppColors.textDim;
  }
}

String runStatusLabel(String status) {
  switch (status) {
    case 'active':
      return '进行中';
    case 'paused':
      return '已暂停';
    case 'pending':
      return '待开始';
    case 'completed':
      return '已结束';
    case 'abandoned':
      return '已放弃';
    default:
      return '未知状态';
  }
}

String assignmentText(Map<String, dynamic> session) {
  final room = textOf(session['room_name'], fallback: '待排房');
  final dm = textOf(session['dm_name'], fallback: '待排 DM');
  return '$room · $dm';
}
