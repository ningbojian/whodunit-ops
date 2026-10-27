import 'package:flutter/material.dart';

import '../core/theme.dart';

/// 聊天气泡下的动作按钮（薄 chip）。
///
/// pending=true 时禁用（上一动作未完成）；按钮是否可直连业务 API
/// 由调用方决定（quick_reply 重发文本，其余 POST 业务端点）。
class ChoiceButton extends StatelessWidget {
  const ChoiceButton({
    super.key,
    required this.label,
    required this.onPressed,
    this.pending = false,
  });

  final String label;
  final VoidCallback? onPressed;
  final bool pending;

  @override
  Widget build(BuildContext context) {
    return ActionChip(
      label: Text(
        label,
        style: const TextStyle(color: AppColors.textDim, fontSize: 12),
      ),
      backgroundColor: AppColors.bg2.withValues(alpha: 0.7),
      side: const BorderSide(color: AppColors.line),
      onPressed: pending ? null : onPressed,
    );
  }
}
