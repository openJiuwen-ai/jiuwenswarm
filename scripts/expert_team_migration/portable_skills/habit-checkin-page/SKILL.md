---
name: habit-checkin-page
description: Turn a finite habit, health, or learning plan into a static-first check-in and review page without relying on Clawdbot storage, WhatsApp reminders, autonomous wakeups, or hidden long-term memory. Use for 7-day, 21-day, 30-day, and weekly trackers with optional browser-local interactions.
---

# Habit Check-in Page

Define the tracker before building it:

1. Give every item a stable label, cadence, observable completion rule, and safe target.
2. Include the complete initial schedule, daily rows, weekly review prompts, and recovery
   rule in the HTML DOM. Never generate core rows only with JavaScript.
3. Keep checkboxes, filters, streak calculations, and `localStorage` as optional
   progressive enhancement. Explain that browser-local state stays in that browser and
   is not a cloud reminder service.
4. Use supportive review language. A missed day changes the next action; it does not
   trigger shame, diagnosis, or unsafe compensation.
5. Hand the finished content or page to the leader as an internal artifact. Do not send
   intermediate spreadsheets, scripts, JSON, or test files to the user.

Do not read or write data under a Skill installation directory. Do not invoke OpenClaw,
Clawdbot, WhatsApp, cron, autonomous workers, or a memory store. If the user explicitly
requests scheduled reminders, report that as a separate runtime capability requirement.
