using System;
using System.Collections.Generic;
using System.Linq;

namespace ComputerUse.Windows {
    internal struct NativePointerPoint {
        internal readonly int X, Y;
        internal NativePointerPoint(int x, int y) { X = x; Y = y; }
    }
    // Retain metadata, never PNG bytes or an inferred UIA target. Scope and
    // transforms cannot be refreshed implicitly after the model sees an image.
    internal sealed class NativePointerCapture {
        internal readonly Dictionary<string, object> Binding;
        internal readonly NativeCaptureGeometry Image, Canvas, WindowBounds;
        internal readonly string Layout;
        readonly NativeCaptureGeometry[] displays;
        internal NativePointerCapture(Dictionary<string, object> binding, NativeCaptureGeometry image,
                NativeCaptureGeometry canvas, string layout, NativeCaptureGeometry[] displays, NativeCaptureGeometry windowBounds = null) {
            Binding = new Dictionary<string, object>(binding); Image = image; Canvas = canvas; Layout = layout;
            WindowBounds = windowBounds ?? image;
            this.displays = (NativeCaptureGeometry[])displays.Clone();
        }
        internal static bool Contains(NativeCaptureGeometry box, NativePointerPoint p) {
            return p.X >= box.X && p.Y >= box.Y && (long)p.X < (long)box.X + box.NativeWidth && (long)p.Y < (long)box.Y + box.NativeHeight;
        }
        internal void RequirePoint(NativePointerPoint p) {
            if (!Contains(Canvas, p) || !displays.Any(d => Contains(d, p)))
                throw new Failure("invalid_request", "pointer trajectory leaves the captured displays");
        }
        internal NativePointerPoint Point(double x, double y) {
            if (Double.IsNaN(x) || Double.IsNaN(y) || x < 0 || y < 0 || x >= Image.Width || y >= Image.Height)
                throw new Failure("invalid_request", "coordinates are outside the retained image");
            var p = new NativePointerPoint(Image.X + (int)Math.Floor(x * Image.NativeWidth / Image.Width),
                Image.Y + (int)Math.Floor(y * Image.NativeHeight / Image.Height));
            RequirePoint(p); return p;
        }
        internal NativePointerPoint Normalize(NativePointerPoint point) {
            RequirePoint(point);
            // Aim at the center of the physical pixel's 16-bit interval. This
            // avoids rounding onto an adjacent pixel on scaled/multiple screens.
            return new NativePointerPoint((int)Math.Floor(((double)point.X - Canvas.X + .5) * 65536 / Canvas.NativeWidth),
                (int)Math.Floor(((double)point.Y - Canvas.Y + .5) * 65536 / Canvas.NativeHeight));
        }
    }
    internal sealed class NativePointerCaptures {
        readonly Dictionary<string, NativePointerCapture> captures = new Dictionary<string, NativePointerCapture>();
        readonly Queue<string> order = new Queue<string>();
        readonly int capacity;
        internal NativePointerCaptures(int capacity = 64) {
            if (capacity < 1) throw new ArgumentOutOfRangeException("capacity"); this.capacity = capacity;
        }
        internal void Add(string id, Dictionary<string, object> binding, NativeCaptureGeometry image,
                NativeCaptureGeometry canvas, string layout, NativeCaptureGeometry[] displays, NativeCaptureGeometry windowBounds = null) {
            if (binding["capture"] != null || binding["node"] != null || captures.ContainsKey(id))
                throw new Failure("invalid_request", "capture retention requires a new window or desktop read binding");
            var target = new Dictionary<string, object>(binding); target["capture"] = id;
            captures.Add(id, new NativePointerCapture(target, image, canvas, layout, displays, windowBounds)); order.Enqueue(id);
            while (order.Count > capacity) captures.Remove(order.Dequeue());
        }
        internal NativePointerCapture Get(Dictionary<string, object> target) {
            NativePointerCapture capture;
            if (target["capture"] == null || !captures.TryGetValue((string)target["capture"], out capture))
                throw new Failure("snapshot_expired", "the original image has expired; read and view a new image");
            if (!NativeWriteOperation.Same(capture.Binding, target))
                throw new Failure("stale_target", "the image belongs to another window or input scope");
            return capture;
        }
    }
}
