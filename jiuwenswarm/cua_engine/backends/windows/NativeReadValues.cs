using System;

namespace ComputerUse.Windows {
    internal static class NativeReadValues {
        // RangeValue LargeChange/SmallChange may legally return quiet NaN.
        // JSON has no NaN or infinity literal: preserve the native fact with
        // a type tag instead of corrupting the entire observation frame.
        internal static object Number(double value) {
            if (!Double.IsNaN(value) && !Double.IsInfinity(value)) return value;
            return Wire.Obj("nativeType", "UIA.Double", "value", Double.IsNaN(value) ? "NaN" : value > 0 ? "+Infinity" : "-Infinity");
        }
    }
}
