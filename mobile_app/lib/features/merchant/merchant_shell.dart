import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../../core/api_client.dart';
import '../../core/auth_store.dart';
import '../../core/session.dart';
import '../../core/theme.dart';
import 'chat_screen.dart';
import 'home_screen.dart';
import 'imports_screen.dart';
import 'profile_screen.dart';
import 'schedule_screen.dart';

class MerchantShell extends StatefulWidget {
  const MerchantShell({
    super.key,
    required this.session,
    required this.api,
    required this.auth,
  });

  final AppSession session;
  final ApiClient api;
  final AuthStore auth;

  @override
  State<MerchantShell> createState() => _MerchantShellState();
}

class _MerchantShellState extends State<MerchantShell> {
  int _index = 0;

  /// 排期密度：compact | timeline（SharedPreferences key "schedule_density"）。
  String _scheduleDensity = 'compact';

  static const _titles = ['今日', '对话', '排期', '渠道单', '我的'];

  @override
  void initState() {
    super.initState();
    _loadDensity();
  }

  Future<void> _loadDensity() async {
    final prefs = await SharedPreferences.getInstance();
    if (!mounted) {
      return;
    }
    setState(() {
      _scheduleDensity = prefs.getString('schedule_density') ?? 'compact';
    });
  }

  Future<void> _setDensity(String value) async {
    setState(() => _scheduleDensity = value);
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('schedule_density', value);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text(_titles[_index]),
        actions: [
          if (_index == 2)
            PopupMenuButton<String>(
              tooltip: '排期密度',
              icon: const Icon(Icons.view_agenda_outlined),
              initialValue: _scheduleDensity,
              onSelected: _setDensity,
              itemBuilder: (context) => const [
                PopupMenuItem(value: 'compact', child: Text('紧凑列表')),
                PopupMenuItem(value: 'timeline', child: Text('时间轴')),
              ],
            ),
          Padding(
            padding: const EdgeInsets.only(right: 16),
            child: Center(
              child: Container(
                padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
                decoration: BoxDecoration(
                  color: AppColors.cyan.withValues(alpha: 0.10),
                  borderRadius: BorderRadius.circular(999),
                  border: Border.all(color: AppColors.cyan.withValues(alpha: 0.30)),
                ),
                child: const Text(
                  '商家',
                  style: TextStyle(
                    color: AppColors.cyan,
                    fontSize: 11,
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ),
            ),
          ),
        ],
      ),
      body: IndexedStack(
        index: _index,
        children: [
          HomeScreen(
            api: widget.api,
            onOpenChat: () => setState(() => _index = 1),
            onOpenSchedule: () => setState(() => _index = 2),
          ),
          ChatScreen(api: widget.api),
          ScheduleScreen(api: widget.api, density: _scheduleDensity),
          ImportsScreen(api: widget.api),
          ProfileScreen(
            api: widget.api,
            auth: widget.auth,
            session: widget.session,
          ),
        ],
      ),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _index,
        onDestinationSelected: (index) => setState(() => _index = index),
        backgroundColor: AppColors.bg1,
        indicatorColor: AppColors.cyan.withValues(alpha: 0.16),
        height: 68,
        labelBehavior: NavigationDestinationLabelBehavior.alwaysShow,
        destinations: const [
          NavigationDestination(
            icon: Icon(Icons.today_outlined),
            selectedIcon: Icon(Icons.today),
            label: '今日',
          ),
          NavigationDestination(
            icon: Icon(Icons.forum_outlined),
            selectedIcon: Icon(Icons.forum),
            label: '对话',
          ),
          NavigationDestination(
            icon: Icon(Icons.calendar_month_outlined),
            selectedIcon: Icon(Icons.calendar_month),
            label: '排期',
          ),
          NavigationDestination(
            icon: Icon(Icons.upload_file_outlined),
            selectedIcon: Icon(Icons.upload_file),
            label: '渠道单',
          ),
          NavigationDestination(
            icon: Icon(Icons.person_outline),
            selectedIcon: Icon(Icons.person),
            label: '我的',
          ),
        ],
      ),
    );
  }
}
