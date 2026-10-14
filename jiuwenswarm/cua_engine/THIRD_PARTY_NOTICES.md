# Third-party notices

The macOS window-scoped input implementation in `BackgroundEvents.swift` uses
event field definitions and an event-record layout described by
[trycua/cua](https://github.com/trycua/cua/tree/5fcd67326dd0406bca823e5488cf39f47491c869/libs/cua-driver/rust/crates/platform-macos/src/input).
It does not require the Cua service or SDK.

The exact-window key event records also follow the layout in
[Cua's SkyLight bridge](https://github.com/trycua/cua/blob/0b88ae17f7c0f389d2a0dfcc1e4f4c0793e03f4b/libs/cua-driver/rust/crates/platform-macos/src/input/skylight.rs).
Our implementation uses the exact-window key records and front-process switch
only for exclusive input or host-authorized foreground assist. The shared route
prepares input in the background first; authorized foreground assist is only a
pre-dispatch fallback. It does not restore the previous application or window.

MIT License

Copyright (c) 2025 Cua AI, Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
