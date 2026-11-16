using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

public sealed class LauncherConsole : IDisposable
{
    [StructLayout(LayoutKind.Explicit, Size = 20)]
    private struct InputRecord
    {
        [FieldOffset(0)] public ushort EventType;
        [FieldOffset(4)] public int KeyDown;
        [FieldOffset(10)] public ushort VirtualKey;
        [FieldOffset(14)] public ushort Character;
        [FieldOffset(16)] public uint ControlKeyState;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateFileW(string name, uint access,
        uint share, IntPtr security, uint creation, uint flags, IntPtr template);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetConsoleMode(SafeFileHandle input, out uint mode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetConsoleMode(SafeFileHandle input, uint mode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetNumberOfConsoleInputEvents(SafeFileHandle input, out uint count);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool ReadConsoleInputW(SafeFileHandle input,
        out InputRecord record, uint length, out uint count);

    private readonly SafeFileHandle input;
    private readonly uint originalMode;

    public LauncherConsole()
    {
        input = CreateFileW("CONIN$", 0xC0000000, 3, IntPtr.Zero, 3, 0, IntPtr.Zero);
        if (input.IsInvalid || !GetConsoleMode(input, out originalMode))
        {
            int error = Marshal.GetLastWin32Error();
            input.Dispose();
            throw new Win32Exception(error);
        }
        // Deliver Ctrl+C as input and disable Quick Edit while the launcher owns input.
        if (!SetConsoleMode(input, (originalMode | 0x80u) & ~0x41u))
        {
            int error = Marshal.GetLastWin32Error();
            input.Dispose();
            throw new Win32Exception(error);
        }
    }

    public bool StopRequested()
    {
        uint available;
        if (!GetNumberOfConsoleInputEvents(input, out available))
            throw new Win32Exception(Marshal.GetLastWin32Error());
        // Bound reads to the observed queue so continuous mouse events cannot starve callers.
        for (uint index = 0; index < available; index++)
        {
            InputRecord record;
            uint count;
            if (!ReadConsoleInputW(input, out record, 1, out count))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            if (count == 1 && record.EventType == 1 && record.KeyDown != 0 &&
                (record.VirtualKey == 13 || record.Character == 3 ||
                 (record.VirtualKey == 67 && (record.ControlKeyState & 0xCu) != 0)))
                return true;
        }
        return false;
    }

    public void Dispose()
    {
        if (!input.IsClosed)
        {
            SetConsoleMode(input, originalMode);
            input.Dispose();
        }
    }
}
