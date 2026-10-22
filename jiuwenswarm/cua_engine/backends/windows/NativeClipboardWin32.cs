using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

namespace ComputerUse.Windows {
    internal sealed class NativeWindowsClipboard : INativeClipboardProvider {
        public NativeClipboardTransaction Save(Request r) { return new NativeClipboardTransaction(new NativeClipboardWin32(), r); }
    }
    // Immediate HGLOBAL formats only: no STA/OLE proxy, delayed owner, file,
    // window activation or application-specific behavior. The message-only
    // owner exists on the operation thread and has no visible surface.
    internal sealed class NativeClipboardWin32 : INativeClipboardApi {
        const long MaxBytes = 16 * 1024 * 1024;
        readonly IntPtr owner;
        bool opened;
        internal NativeClipboardWin32() {
            owner = CreateWindowEx(0, "STATIC", "", 0, 0, 0, 0, 0, new IntPtr(-3), IntPtr.Zero, IntPtr.Zero, IntPtr.Zero);
            if (owner == IntPtr.Zero) throw Error("could not create clipboard transaction owner");
        }
        static Failure Error(string detail) { return new Failure("input_unavailable", detail + ": " + new Win32Exception(Marshal.GetLastWin32Error()).Message); }
        sealed class Lock : IDisposable {
            readonly NativeClipboardWin32 api;
            internal Lock(NativeClipboardWin32 api) { this.api = api; }
            public void Dispose() { if (!api.opened) return; if (!CloseClipboard()) throw Error("could not release clipboard lock"); api.opened = false; }
        }
        public IDisposable Open(Request r) {
            if (opened) throw new Failure("invalid_request", "clipboard transaction lock is already held");
            double until = Math.Min(r.Deadline, Wire.Now + 400);
            while (true) {
                r.Check();
                if (OpenClipboard(owner)) { opened = true; return new Lock(this); }
                if (Wire.Now >= until) throw Error("clipboard is busy; transaction did not acquire it");
                r.Wait(10);
            }
        }
        void RequireOpen() { if (!opened) throw new Failure("invalid_request", "clipboard access requires its native lock"); }
        public uint Version {
            get { RequireOpen(); uint value = GetClipboardSequenceNumber(); if (value == 0) throw Error("clipboard version is unavailable"); return value; }
        }
        static bool GlobalFormat(uint format) {
            // Private/GDI/owner-display formats have other handle lifetimes.
            // Do not flatten or destroy them just to make paste available.
            return new uint[] { 1, 4, 5, 6, 7, 8, 10, 11, 12, 13, 15, 16, 17, 0x81 }.Contains(format) || format >= 0xC000 && format <= 0xFFFF;
        }
        public IList<NativeClipboardData> Read(Request r) {
            RequireOpen(); var entries = new List<NativeClipboardData>(); var seen = new HashSet<uint>(); long bytes = 0; uint format = 0;
            while (true) {
                r.Check(); SetLastError(0); format = EnumClipboardFormats(format);
                if (format == 0) { if (Marshal.GetLastWin32Error() != 0) throw Error("clipboard format enumeration failed"); break; }
                if (!seen.Add(format) || entries.Count >= 256) throw new Failure("input_unavailable", "clipboard formats exceed the bounded preservation profile; no paste sent");
                if (!GlobalFormat(format)) throw new Failure("input_unavailable", "clipboard format " + format + " cannot be preserved by this transaction; no paste sent");
                IntPtr data = GetClipboardData(format); r.Check();
                if (data == IntPtr.Zero) throw Error("clipboard format could not be materialized");
                ulong size = GlobalSize(data).ToUInt64();
                if (size == 0 || size > (ulong)(MaxBytes - bytes)) throw new Failure("input_unavailable", "clipboard data exceeds the preservation budget or has no safe memory representation; no paste sent");
                var saved = new byte[(int)size]; IntPtr pointer = GlobalLock(data);
                if (pointer == IntPtr.Zero) throw Error("clipboard data could not be locked");
                try { Marshal.Copy(pointer, saved, 0, saved.Length); } finally { GlobalUnlock(data); }
                entries.Add(new NativeClipboardData(format, saved)); bytes += saved.Length;
            }
            return entries;
        }
        sealed class Payload : INativeClipboardPayload {
            internal readonly List<Tuple<uint, IntPtr>> Items = new List<Tuple<uint, IntPtr>>();
            public void Dispose() {
                bool failed = false;
                foreach (var item in Items) if (item.Item2 != IntPtr.Zero && GlobalFree(item.Item2) != IntPtr.Zero) failed = true;
                Items.Clear(); if (failed) throw Error("clipboard allocation could not be released");
            }
        }
        public INativeClipboardPayload Prepare(IList<NativeClipboardData> entries, Request r) {
            var result = new Payload(); long total = 0;
            try {
                foreach (var entry in entries) {
                    r.Check(); total += entry.Bytes.Length;
                    if (result.Items.Count >= 256 || entry.Bytes.Length == 0 || total > MaxBytes || !GlobalFormat(entry.Format))
                        throw new Failure("input_unavailable", "clipboard payload cannot be fully prepared");
                    IntPtr handle = GlobalAlloc(2, new UIntPtr((uint)entry.Bytes.Length));
                    if (handle == IntPtr.Zero) throw Error("clipboard allocation failed");
                    result.Items.Add(Tuple.Create(entry.Format, handle));
                    IntPtr pointer = GlobalLock(handle); if (pointer == IntPtr.Zero) throw Error("clipboard allocation could not be locked");
                    try { Marshal.Copy(entry.Bytes, 0, pointer, entry.Bytes.Length); } finally { GlobalUnlock(handle); }
                }
                return result;
            } catch { result.Dispose(); throw; }
        }
        static Encoding TextEncoding(uint locale, uint property, uint fallback) {
            var value = new StringBuilder(16); uint codepage;
            if (GetLocaleInfo(locale, property, value, value.Capacity) == 0 || !UInt32.TryParse(value.ToString(), out codepage))
                throw Error("clipboard text encoding is unavailable");
            return Encoding.GetEncoding((int)(codepage == 0 ? fallback : codepage));
        }
        public INativeClipboardPayload PrepareText(string text, Request r) {
            r.Check(); uint locale = GetThreadLocale();
            return Prepare(NativeClipboardText.Formats(text, locale,
                TextEncoding(locale, 0x1004, GetACP()), TextEncoding(locale, 0x000b, GetOEMCP())), r);
        }
        public void Replace(INativeClipboardPayload value, Action changed, Request r) {
            RequireOpen(); var payload = value as Payload;
            if (payload == null) throw new Failure("invalid_request", "foreign clipboard payload");
            r.Check(); if (!EmptyClipboard()) throw Error("clipboard could not be cleared"); changed();
            foreach (int index in Enumerable.Range(0, payload.Items.Count)) {
                r.Check(); var item = payload.Items[index];
                if (item.Item2 == IntPtr.Zero) throw new Failure("invalid_request", "clipboard memory has already transferred");
                if (SetClipboardData(item.Item1, item.Item2) == IntPtr.Zero) throw Error("clipboard format could not be installed");
                payload.Items[index] = Tuple.Create(item.Item1, IntPtr.Zero); // The OS owns this allocation now.
            }
        }
        public void Handoff() { Thread.Sleep(500); } // Bounded and independent of request cancellation, not an acknowledgement.
        public void Dispose() {
            bool failed = false;
            if (opened) { if (!CloseClipboard()) failed = true; else opened = false; }
            if (!DestroyWindow(owner)) failed = true;
            if (failed) throw Error("clipboard transaction resources could not be released");
        }
        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern IntPtr CreateWindowEx(uint exStyle, string name, string title, uint style, int x, int y, int width, int height, IntPtr parent, IntPtr menu, IntPtr instance, IntPtr param);
        [DllImport("user32.dll", SetLastError = true)] static extern bool DestroyWindow(IntPtr window);
        [DllImport("user32.dll", SetLastError = true)] static extern bool OpenClipboard(IntPtr owner);
        [DllImport("user32.dll", SetLastError = true)] static extern bool CloseClipboard();
        [DllImport("user32.dll", SetLastError = true)] static extern bool EmptyClipboard();
        [DllImport("user32.dll", SetLastError = true)] static extern uint EnumClipboardFormats(uint previous);
        [DllImport("user32.dll", SetLastError = true)] static extern IntPtr GetClipboardData(uint format);
        [DllImport("user32.dll", SetLastError = true)] static extern IntPtr SetClipboardData(uint format, IntPtr data);
        [DllImport("user32.dll", SetLastError = true)] static extern uint GetClipboardSequenceNumber();
        [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr GlobalAlloc(uint flags, UIntPtr bytes);
        [DllImport("kernel32.dll", SetLastError = true)] static extern UIntPtr GlobalSize(IntPtr handle);
        [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr GlobalLock(IntPtr handle);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool GlobalUnlock(IntPtr handle);
        [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr GlobalFree(IntPtr handle);
        [DllImport("kernel32.dll")] static extern void SetLastError(uint code);
        [DllImport("kernel32.dll")] static extern uint GetThreadLocale();
        [DllImport("kernel32.dll")] static extern uint GetACP();
        [DllImport("kernel32.dll")] static extern uint GetOEMCP();
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern int GetLocaleInfo(uint locale, uint type, StringBuilder value, int count);
    }
}
