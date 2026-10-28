using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Linq;
using System.Windows.Forms;

namespace ComputerUse.Windows {
    // Read-only pixels. No activation, UIA traversal,
    // clipboard change or legacy Backend/Frame is involved in this adapter.
    internal sealed class NativeCapture : INativeCaptureBackend {
        readonly NativeReadStore store;
        readonly NativePointerCaptures captures;
        readonly string desktopScope, mode;
        internal NativeCapture(NativeReadStore store, string session, string mode, NativePointerCaptures captures = null) {
            this.store = store; desktopScope = session + ":desktop"; this.mode = mode; this.captures = captures;
        }
        static void Check(Request r) {
            r.Check();
            if (!Native.Interactive()) throw new Failure("desktop_unavailable", "capture requires the unlocked interactive desktop");
        }
        static object Stamp(double start) {
            return Wire.Obj("id", NativeReadStore.Token("observation"), "startedAt", Math.Floor(start), "completedAt", Math.Ceiling(Wire.Now));
        }
        static string DesktopPixels(Rectangle physical, NativeCaptureGeometry geometry, Request r) {
            using (var original = new Bitmap(physical.Width, physical.Height, PixelFormat.Format32bppRgb)) {
                using (var graphics = Graphics.FromImage(original))
                    graphics.CopyFromScreen(physical.Location, Point.Empty, physical.Size);
                r.Check();
                using (var scaled = new Bitmap(original, geometry.Width, geometry.Height))
                using (var stream = new MemoryStream()) {
                    scaled.Save(stream, ImageFormat.Png); r.Check();
                    if (stream.Length > 32 * 1024 * 1024) throw new Failure("observation_unavailable", "encoded screenshot exceeds the wire image budget");
                    return Convert.ToBase64String(stream.ToArray());
                }
            }
        }
        static Dictionary<string, object> Image(string kind, string scope, double start, NativeCaptureGeometry geometry, string png) {
            return Wire.Obj("id", NativeReadStore.Token("capture"), "kind", kind, "scope", scope,
                "startedAt", Math.Floor(start), "completedAt", Math.Ceiling(Wire.Now),
                "width", geometry.Width, "height", geometry.Height, "png", png);
        }
        public object Capture(Dictionary<string, object> target, string kind, int maxDimension, Request r) {
            Check(r); double start = Wire.Now;
            if (kind == "window") {
                var selected = store.Validate(target, r);
                if (selected == null || target["node"] != null || target["capture"] != null)
                    throw new Failure("invalid_request", "window capture requires a window read binding");
                Window window = selected.Native;
                var layout = captures == null ? null : new NativePointerLayout();
                Rectangle physical = Native.Bounds(window.Handle);
                var windowPixels = NativeWindowPixels.Read(window, maxDimension, r);
                var geometry = windowPixels.Geometry;
                string png = windowPixels.Png;
                store.Validate(target, r); Check(r);
                if (Native.Bounds(window.Handle) != physical || Native.IsIconic(window.Handle))
                    throw new Failure("stale_target", "window geometry changed during capture; read again");
                var windowImage = Image(kind, (string)target["surface"], start, geometry, png);
                if (windowPixels.Notes.Length > 0) windowImage["stopReasons"] = windowPixels.Notes;
                if (captures != null) {
                    if (new NativePointerLayout().Signature != layout.Signature) throw new Failure("stale_target", "display layout changed during window capture; read again");
                    captures.Add((string)windowImage["id"], target, geometry, layout.Canvas, layout.Signature, layout.Displays,
                        new NativeCaptureGeometry(physical.X, physical.Y, physical.Width, physical.Height, maxDimension));
                }
                return Wire.Obj("kind", "capture", "binding", new Dictionary<string, object>(target), "observed", Stamp(start),
                    "image", windowImage, "coordinates", geometry.Coordinates());
            }
            if (kind != "desktop" || target.Any(p => p.Key != "session" && p.Value != null))
                throw new Failure("invalid_request", "desktop capture requires a session read binding");
            Rectangle canvas = SystemInformation.VirtualScreen;
            var desktopLayout = captures == null ? null : new NativePointerLayout();
            var shape = new NativeCaptureGeometry(canvas.X, canvas.Y, canvas.Width, canvas.Height, maxDimension);
            var displaysBefore = Screen.AllScreens.Select(s => new { Name = s.DeviceName, Bounds = s.Bounds }).ToArray();
            if (displaysBefore.Length < 1 || displaysBefore.Length > 32 || displaysBefore.Select(s => s.Name).Distinct().Count() != displaysBefore.Length
                    || displaysBefore.Any(s => shape.Clip(s.Bounds.X, s.Bounds.Y, s.Bounds.Width, s.Bounds.Height) == null))
                throw new Failure("observation_unavailable", "display catalogue is incomplete or exceeds the protocol budget");
            string pixels = DesktopPixels(canvas, shape, r);
            if (SystemInformation.VirtualScreen != canvas || !displaysBefore.Select(s => s.Name + ":" + s.Bounds)
                    .SequenceEqual(Screen.AllScreens.Select(s => s.DeviceName + ":" + s.Bounds)))
                throw new Failure("stale_target", "display geometry changed during capture; read again");
            var notes = new List<string>(); var windows = new List<object>(); object frontmost = null;
            try {
                IntPtr front = Native.GetForegroundWindow();
                foreach (var item in store.Catalog(r, notes)) {
                    r.Check();
                    try {
                        Window window = item.Native; window.Validate();
                        if (Native.IsIconic(window.Handle)) continue;
                        Rectangle bounds = Native.Bounds(window.Handle);
                        var rect = shape.Clip(bounds.X, bounds.Y, bounds.Width, bounds.Height);
                        if (rect == null) continue;
                        windows.Add(Wire.Obj("app", NativeReadStore.Text(window.Name), "title", NativeReadStore.Text(Native.Title(window.Handle)),
                            "selector", window.Selector, "rect", rect));
                        if (window.Handle == front) frontmost = Wire.Obj("name", NativeReadStore.Text(window.Name), "selector", window.Selector);
                    } catch (Failure error) {
                        if (error.Code == "cancelled" || error.Code == "deadline_exceeded") throw;
                        NativeReadStore.Note(notes, "Window metadata changed after pixels were captured: " + error.Message);
                    } catch (Exception error) { r.Check(); NativeReadStore.Note(notes, "Window metadata unavailable: " + error.Message); }
                }
            } catch (Failure error) {
                if (error.Code == "cancelled" || error.Code == "deadline_exceeded") throw;
                NativeReadStore.Note(notes, "Desktop catalogue unavailable: " + error.Message);
            } catch (Exception error) { r.Check(); NativeReadStore.Note(notes, "Desktop catalogue unavailable: " + error.Message); }
            Check(r);
            var displays = displaysBefore.Select(s => Wire.Obj("name", s.Name,
                "rect", shape.Clip(s.Bounds.X, s.Bounds.Y, s.Bounds.Width, s.Bounds.Height), "captured", true)).ToArray();
            var observation = Wire.Obj("mode", mode, "canvas", Wire.Obj("width", shape.Width, "height", shape.Height),
                "frontmost", frontmost, "displays", displays, "windows", windows.Take(256).ToArray(), "omittedWindows", Math.Max(0, windows.Count-256));
            if (notes.Count > 0) observation["catalogError"] = NativeReadStore.Text(String.Join("\n", notes));
            var image = Image(kind, desktopScope, start, shape, pixels);
            if (captures != null) {
                var layout = new NativePointerLayout();
                if (layout.Signature != desktopLayout.Signature || layout.Canvas.X != canvas.X || layout.Canvas.Y != canvas.Y || layout.Canvas.NativeWidth != canvas.Width || layout.Canvas.NativeHeight != canvas.Height
                    || !displaysBefore.Select(s => s.Name + ":" + s.Bounds).SequenceEqual(Screen.AllScreens.Select(s => s.DeviceName + ":" + s.Bounds)))
                    throw new Failure("stale_target", "display geometry changed during capture; read again");
                captures.Add((string)image["id"], target, shape, layout.Canvas, layout.Signature, layout.Displays);
            }
            return Wire.Obj("kind", "desktopCapture", "binding", new Dictionary<string, object>(target), "desktopRef", image["id"],
                "scope", desktopScope, "observed", Stamp(start), "observation", observation, "screenshot", image);
        }
    }
}
