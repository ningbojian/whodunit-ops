import 'package:flutter/material.dart';

/// 与 Web 端共用的视觉语义 token（不共享代码）。
///
/// 深墨背景 + 青色/紫色霓虹；状态永远同时有中文文字，不只靠颜色。
class AppColors {
  static const bg0 = Color(0xFF05070F);
  static const bg1 = Color(0xFF0A0F1E);
  static const bg2 = Color(0xFF111832);
  static const line = Color(0x2E6EB4FF);
  static const lineStrong = Color(0x616EB4FF);
  static const text = Color(0xFFE8F0FF);
  static const textDim = Color(0xFF8FA0C4);
  static const textFaint = Color(0xFF5D6C8C);
  static const cyan = Color(0xFF22D3EE);
  static const violet = Color(0xFF8B5CF6);
  static const pink = Color(0xFFF472B6);
  static const mint = Color(0xFF34D399);
  static const amber = Color(0xFFFBBF24);
  static const danger = Color(0xFFFB7185);
}

ThemeData buildAppTheme() {
  final baseScheme = ColorScheme.fromSeed(
    seedColor: AppColors.cyan,
    brightness: Brightness.dark,
  );
  final scheme = baseScheme.copyWith(
    primary: AppColors.cyan,
    onPrimary: AppColors.bg0,
    secondary: AppColors.violet,
    onSecondary: Colors.white,
    error: AppColors.danger,
    onError: AppColors.bg0,
    surface: AppColors.bg1,
    onSurface: AppColors.text,
  );

  return ThemeData(
    useMaterial3: true,
    brightness: Brightness.dark,
    colorScheme: scheme,
    scaffoldBackgroundColor: AppColors.bg0,
    canvasColor: AppColors.bg0,
    dividerColor: AppColors.line,
    splashFactory: InkRipple.splashFactory,
    appBarTheme: const AppBarTheme(
      backgroundColor: AppColors.bg0,
      surfaceTintColor: Colors.transparent,
      foregroundColor: AppColors.text,
      elevation: 0,
      centerTitle: false,
      titleTextStyle: TextStyle(
        color: AppColors.text,
        fontSize: 18,
        fontWeight: FontWeight.w700,
        letterSpacing: 0.4,
      ),
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: AppColors.bg1,
      hintStyle: const TextStyle(color: AppColors.textFaint),
      labelStyle: const TextStyle(color: AppColors.textDim),
      contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(14),
        borderSide: const BorderSide(color: AppColors.line),
      ),
      enabledBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(14),
        borderSide: const BorderSide(color: AppColors.line),
      ),
      focusedBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(14),
        borderSide: const BorderSide(color: AppColors.cyan, width: 1.4),
      ),
      errorBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(14),
        borderSide: const BorderSide(color: AppColors.danger),
      ),
      focusedErrorBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(14),
        borderSide: const BorderSide(color: AppColors.danger, width: 1.4),
      ),
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        backgroundColor: AppColors.cyan,
        foregroundColor: AppColors.bg0,
        minimumSize: const Size(56, 52),
        textStyle: const TextStyle(fontSize: 16, fontWeight: FontWeight.w700),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(14),
        ),
      ),
    ),
    textButtonTheme: TextButtonThemeData(
      style: TextButton.styleFrom(
        foregroundColor: AppColors.cyan,
        minimumSize: const Size(48, 44),
      ),
    ),
    outlinedButtonTheme: OutlinedButtonThemeData(
      style: OutlinedButton.styleFrom(
        foregroundColor: AppColors.text,
        minimumSize: const Size(48, 48),
        side: const BorderSide(color: AppColors.lineStrong),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(14),
        ),
      ),
    ),
    snackBarTheme: const SnackBarThemeData(
      backgroundColor: AppColors.bg2,
      contentTextStyle: TextStyle(color: AppColors.text),
      behavior: SnackBarBehavior.floating,
    ),
    progressIndicatorTheme: const ProgressIndicatorThemeData(
      color: AppColors.cyan,
      linearTrackColor: AppColors.bg2,
    ),
    textTheme: const TextTheme(
      headlineSmall: TextStyle(
        color: AppColors.text,
        fontWeight: FontWeight.w700,
        letterSpacing: 0.2,
      ),
      titleMedium: TextStyle(
        color: AppColors.text,
        fontWeight: FontWeight.w600,
      ),
      bodyMedium: TextStyle(color: AppColors.textDim, height: 1.45),
      bodySmall: TextStyle(color: AppColors.textFaint),
    ),
  );
}
