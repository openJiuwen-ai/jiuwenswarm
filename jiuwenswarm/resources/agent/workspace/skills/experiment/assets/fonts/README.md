# Optional bundled fonts

Module 3 first registers `.ttf`, `.otf` and `.ttc` files in this directory,
then falls back to fonts installed on the operating system.

For portable Chinese rendering, place a redistributable Simplified Chinese
font here, preferably `Noto Sans SC` under its original SIL Open Font License.
Do not add a commercial font unless its licence explicitly permits repository
redistribution.

The current test machine already provides `Noto Sans SC` and Microsoft YaHei,
so no third-party font binary is copied into the project.
