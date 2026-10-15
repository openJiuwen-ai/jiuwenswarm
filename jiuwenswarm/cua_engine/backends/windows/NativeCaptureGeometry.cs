using System;
using System.Collections.Generic;

namespace ComputerUse.Windows {
    // Screenshot geometry is immutable and independent of UIA or input routing.
    internal sealed class NativeCaptureGeometry {
        internal readonly int X, Y, NativeWidth, NativeHeight, Width, Height;
        internal NativeCaptureGeometry(int x, int y, int nativeWidth, int nativeHeight, int maxDimension) {
            if (nativeWidth < 1 || nativeHeight < 1 || nativeWidth > 32768 || nativeHeight > 32768
                    || (long)nativeWidth * nativeHeight > 64000000)
                throw new Failure("observation_unavailable", "capture geometry exceeds the native pixel budget");
            if (maxDimension < 1 || maxDimension > 4096) throw new Failure("invalid_request", "invalid capture dimension");
            X = x; Y = y; NativeWidth = nativeWidth; NativeHeight = nativeHeight;
            double scale = Math.Min(1.0, (double)maxDimension / Math.Max(nativeWidth, nativeHeight));
            Width = Math.Max(1, (int)Math.Round(nativeWidth * scale));
            Height = Math.Max(1, (int)Math.Round(nativeHeight * scale));
        }
        internal Dictionary<string, object> Coordinates() {
            return Wire.Obj("space", "window-pixels", "origin", "top-left", "nativeSpace", "screen-pixels",
                "toNative", new double[] { (double)NativeWidth / Width, 0, 0, (double)NativeHeight / Height, X, Y });
        }
        internal Dictionary<string, object> Clip(int x, int y, int width, int height) {
            double left = Math.Max(X, (double)x), top = Math.Max(Y, (double)y);
            double right = Math.Min((double)X + NativeWidth, (double)x + width);
            double bottom = Math.Min((double)Y + NativeHeight, (double)y + height);
            if (right <= left || bottom <= top) return null;
            return Wire.Obj("x", (left-X)*Width/NativeWidth, "y", (top-Y)*Height/NativeHeight,
                "width", (right-left)*Width/NativeWidth, "height", (bottom-top)*Height/NativeHeight);
        }
    }
}
