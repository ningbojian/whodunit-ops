import 'package:flutter/material.dart';

import '../../core/api_client.dart';
import '../../core/auth_store.dart';
import '../../core/clock.dart';
import '../../core/notifications.dart';
import '../../core/queue.dart';
import '../../core/session.dart';
import '../../core/theme.dart';
import '../dm/dm_console_screen.dart';
import '../merchant/merchant_shell.dart';
import 'dm_login.dart';
import 'merchant_login.dart';

class RoleGate extends StatelessWidget {
  const RoleGate({
    super.key,
    required this.session,
    required this.api,
    required this.auth,
    required this.queue,
    required this.clock,
    required this.notifications,
  });

  final AppSession session;
  final ApiClient api;
  final AuthStore auth;
  final ActionQueue queue;
  final ServerClock clock;
  final NotificationService notifications;

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: session,
      builder: (context, _) {
        if (!session.ready) {
          return const _Splash();
        }
        switch (session.role) {
          case AppRole.merchant:
            return MerchantShell(
              session: session,
              api: api,
              auth: auth,
            );
          case AppRole.dm:
            return DmConsoleScreen(
              session: session,
              api: api,
              auth: auth,
              queue: queue,
              clock: clock,
              notifications: notifications,
            );
          case AppRole.none:
            return RolePicker(
              session: session,
              api: api,
              auth: auth,
            );
        }
      },
    );
  }
}

class _Splash extends StatelessWidget {
  const _Splash();

  @override
  Widget build(BuildContext context) {
    return const Scaffold(
      body: DecoratedBox(
        decoration: AppBackground.gradient,
        child: Center(
          child: CircularProgressIndicator(),
        ),
      ),
    );
  }
}

class AppBackground {
  static const gradient = BoxDecoration(
    gradient: LinearGradient(
      begin: Alignment.topLeft,
      end: Alignment.bottomRight,
      colors: [AppColors.bg0, AppColors.bg1, Color(0xFF10142B)],
      stops: [0.0, 0.55, 1.0],
    ),
  );
}

class RolePicker extends StatefulWidget {
  const RolePicker({
    super.key,
    required this.session,
    required this.api,
    required this.auth,
  });

  final AppSession session;
  final ApiClient api;
  final AuthStore auth;

  @override
  State<RolePicker> createState() => _RolePickerState();
}

class _RolePickerState extends State<RolePicker> {
  AppRole _entry = AppRole.none;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: DecoratedBox(
        decoration: AppBackground.gradient,
        child: SafeArea(
          child: AnimatedSwitcher(
            duration: const Duration(milliseconds: 180),
            child: switch (_entry) {
              AppRole.merchant => MerchantLoginScreen(
                  key: const ValueKey('merchant-login'),
                  session: widget.session,
                  api: widget.api,
                  auth: widget.auth,
                  onBack: () => setState(() => _entry = AppRole.none),
                ),
              AppRole.dm => DmLoginScreen(
                  key: const ValueKey('dm-login'),
                  session: widget.session,
                  api: widget.api,
                  auth: widget.auth,
                  onBack: () => setState(() => _entry = AppRole.none),
                ),
              AppRole.none => _PickerHome(
                  key: const ValueKey('picker'),
                  onMerchant: () => setState(() => _entry = AppRole.merchant),
                  onDm: () => setState(() => _entry = AppRole.dm),
                ),
            },
          ),
        ),
      ),
    );
  }
}

class _PickerHome extends StatelessWidget {
  const _PickerHome({
    super.key,
    required this.onMerchant,
    required this.onDm,
  });

  final VoidCallback onMerchant;
  final VoidCallback onDm;

  @override
  Widget build(BuildContext context) {
    return SingleChildScrollView(
      padding: const EdgeInsets.fromLTRB(24, 48, 24, 32),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          const Text(
            '迷雾运营台',
            style: TextStyle(
              color: AppColors.text,
              fontSize: 32,
              fontWeight: FontWeight.w800,
              letterSpacing: 1.2,
            ),
          ),
          const SizedBox(height: 8),
          const Text(
            '剧本杀门店运营与 DM 场控',
            style: TextStyle(color: AppColors.textDim, fontSize: 15),
          ),
          const SizedBox(height: 40),
          _RoleCard(
            icon: Icons.storefront_outlined,
            title: '商家工作台',
            description: '对话录入、排期、渠道单与场控码',
            accent: AppColors.cyan,
            onTap: onMerchant,
          ),
          const SizedBox(height: 16),
          _RoleCard(
            icon: Icons.theater_comedy_outlined,
            title: 'DM 场控台',
            description: '输入店长给的场控码，管理本场阶段与计时',
            accent: AppColors.violet,
            onTap: onDm,
          ),
          const SizedBox(height: 32),
          const Row(
            children: [
              Icon(Icons.shield_outlined, size: 16, color: AppColors.textFaint),
              SizedBox(width: 8),
              Expanded(
                child: Text(
                  '场控台只提供计时与提示；倒计时到点不会自动推进剧情，'
                  '阶段永远由 DM 手动切换。',
                  style: TextStyle(color: AppColors.textFaint, fontSize: 12, height: 1.5),
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }
}

class _RoleCard extends StatelessWidget {
  const _RoleCard({
    required this.icon,
    required this.title,
    required this.description,
    required this.accent,
    required this.onTap,
  });

  final IconData icon;
  final String title;
  final String description;
  final Color accent;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Material(
      color: Colors.transparent,
      child: InkWell(
        borderRadius: BorderRadius.circular(18),
        onTap: onTap,
        child: Ink(
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(18),
            color: AppColors.bg2.withValues(alpha: 0.72),
            border: Border.all(color: accent.withValues(alpha: 0.34)),
          ),
          padding: const EdgeInsets.all(20),
          child: Row(
            children: [
              Container(
                width: 48,
                height: 48,
                decoration: BoxDecoration(
                  color: accent.withValues(alpha: 0.14),
                  borderRadius: BorderRadius.circular(14),
                ),
                child: Icon(icon, color: accent, size: 26),
              ),
              const SizedBox(width: 16),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      title,
                      style: const TextStyle(
                        color: AppColors.text,
                        fontSize: 18,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                    const SizedBox(height: 4),
                    Text(
                      description,
                      style: const TextStyle(
                        color: AppColors.textDim,
                        fontSize: 13,
                        height: 1.4,
                      ),
                    ),
                  ],
                ),
              ),
              const Icon(Icons.chevron_right, color: AppColors.textFaint),
            ],
          ),
        ),
      ),
    );
  }
}
