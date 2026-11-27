// ============================================================================
// 时迁主控台 1.1.1 便携版 — 桌面原生独立启动器 (WinForms + 系统托盘 + 进程作业托管)
// 功能说明：
//   1. 支持直接放在桌面独立运行，自动定位便携版本体目录（优先 SHIQIAN_ROOT）。
//   2. 使用便携包内置 runtime\python.exe 启动 launcher.py / console_ui.py。
//   3. 绑定 Windows Job Object (KILL_ON_JOB_CLOSE | BREAKAWAY_OK)，退出时先调
//      POST /api/stop 优雅关停桥接子模块，再清理进程树，确保 0 残留进程。
//   4. 轮询本地服务就绪后，优先用 Edge/Chrome --app 独立窗口打开主控台，并自动为
//      127.0.0.1;localhost;192.168.123.* 配置绕过系统代理（避免 Clash 拦截机器人通信）。
//   5. 提供常驻控制面板与系统托盘图标，支持重复双击自动唤起主控台与 --stop 优雅退出。
// ============================================================================

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;

[assembly: AssemblyTitle("时迁主控台 1.1.1 桌面启动器")]
[assembly: AssemblyDescription("时迁主控台 1.1.1 便携版桌面启动器（本体路径可配置）")]
[assembly: AssemblyProduct("时迁主控台")]
[assembly: AssemblyVersion("1.1.1.0")]
[assembly: AssemblyFileVersion("1.1.1.0")]

namespace ShiqianLauncher
{
    internal static class NativeMethods
    {
        [DllImport("user32.dll")]
        public static extern bool SetProcessDPIAware();
    }

    internal static class NativeJob
    {
        private const int JobObjectExtendedLimitInformation = 9;
        private const uint JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x0800;
        private const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000;

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
        {
            public long PerProcessUserTimeLimit;
            public long PerJobUserTimeLimit;
            public uint LimitFlags;
            public UIntPtr MinimumWorkingSetSize;
            public UIntPtr MaximumWorkingSetSize;
            public uint ActiveProcessLimit;
            public UIntPtr Affinity;
            public uint PriorityClass;
            public uint SchedulingClass;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct IO_COUNTERS
        {
            public ulong ReadOperationCount;
            public ulong WriteOperationCount;
            public ulong OtherOperationCount;
            public ulong ReadTransferCount;
            public ulong WriteTransferCount;
            public ulong OtherTransferCount;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        {
            public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
            public IO_COUNTERS IoInfo;
            public UIntPtr ProcessMemoryLimit;
            public UIntPtr JobMemoryLimit;
            public UIntPtr PeakProcessMemoryUsed;
            public UIntPtr PeakJobMemoryUsed;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateJobObject(IntPtr lpJobAttributes, string lpName);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetInformationJobObject(
            IntPtr hJob,
            int infoType,
            IntPtr lpJobObjectInfo,
            uint cbJobObjectInfoLength);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool AssignProcessToJobObject(IntPtr hJob, IntPtr hProcess);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CloseHandle(IntPtr hObject);

        public static IntPtr CreateKillOnCloseJob()
        {
            IntPtr job = CreateJobObject(IntPtr.Zero, null);
            if (job == IntPtr.Zero) return IntPtr.Zero;

            var info = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
            info.BasicLimitInformation.LimitFlags =
                JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK;

            int length = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
            IntPtr ptr = Marshal.AllocHGlobal(length);
            try
            {
                Marshal.StructureToPtr(info, ptr, false);
                if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation, ptr, (uint)length))
                {
                    CloseHandle(job);
                    return IntPtr.Zero;
                }
            }
            finally
            {
                Marshal.FreeHGlobal(ptr);
            }
            return job;
        }

        public static void Attach(IntPtr job, Process proc)
        {
            if (job != IntPtr.Zero && proc != null && !proc.HasExited)
            {
                AssignProcessToJobObject(job, proc.Handle);
            }
        }

        public static void Close(IntPtr job)
        {
            if (job != IntPtr.Zero) CloseHandle(job);
        }
    }

    internal static class LauncherLog
    {
        private static readonly object Sync = new object();
        private static string _logPath = Path.Combine(Path.GetTempPath(), "shiqian_launcher.log");

        public static string LogPath { get { return _logPath; } }

        public static void Init(string baseRoot)
        {
            try
            {
                string logDir = Path.Combine(baseRoot, "logs");
                Directory.CreateDirectory(logDir);
                _logPath = Path.Combine(logDir, "launcher.log");
            }
            catch
            {
                _logPath = Path.Combine(Path.GetTempPath(), "shiqian_launcher.log");
            }
            Write("INFO", "============================================================");
            Write("INFO", "Shiqian Console Launcher v1.1.1 starting (PID " + Process.GetCurrentProcess().Id + ")");
        }

        public static void Write(string level, string message)
        {
            string line = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss.fff")
                + " [" + level + "] " + message;
            lock (Sync)
            {
                try
                {
                    string dir = Path.GetDirectoryName(_logPath);
                    if (!string.IsNullOrEmpty(dir) && !Directory.Exists(dir))
                    {
                        Directory.CreateDirectory(dir);
                    }
                    File.AppendAllText(_logPath, line + Environment.NewLine, Encoding.UTF8);
                }
                catch
                {
                }
            }
        }
    }

    internal sealed class ConsoleServiceManager : IDisposable
    {
        private readonly object _lock = new object();
        private Process _consoleProcess;
        private IntPtr _jobHandle = IntPtr.Zero;
        private bool _disposed;

        public string OuterRoot { get; private set; }
        public string PkgRoot { get; private set; }
        public string ProjDir { get; private set; }
        public string PythonExe { get; private set; }
        public int ConsolePort { get; private set; }
        public string ConsoleUrl { get { return "http://127.0.0.1:" + ConsolePort; } }
        public string StatusUrl { get { return ConsoleUrl + "/api/status"; } }
        public string StopApiUrl { get { return ConsoleUrl + "/api/stop"; } }

        public static string ResolvePackageRoot(string exeDir)
        {
            var candidates = new List<string>();
            string envRoot = Environment.GetEnvironmentVariable("SHIQIAN_ROOT");
            if (!string.IsNullOrWhiteSpace(envRoot))
            {
                candidates.Add(envRoot.Trim());
                candidates.Add(Path.Combine(envRoot.Trim(), "时迁主控台_1.1.0_便携版"));
            }

            if (!string.IsNullOrEmpty(exeDir))
            {
                candidates.Add(exeDir);
                candidates.Add(Path.Combine(exeDir, "时迁主控台_1.1.0_便携版"));
                candidates.Add(Path.Combine(exeDir, "时迁主控台_1.1.1_便携版", "时迁主控台_1.1.0_便携版"));
                candidates.Add(Path.Combine(exeDir, "时迁主控台_1.1.1_便携版"));
                candidates.Add(Path.Combine(exeDir, ".."));
                candidates.Add(Path.Combine(exeDir, "..", "时迁主控台_1.1.0_便携版"));
            }

            // 常见盘符兜底探测（可用 SHIQIAN_ROOT 覆盖）
            string[] drives = new[] { "E:\\", "D:\\", "F:\\", "C:\\" };
            foreach (string d in drives)
            {
                candidates.Add(Path.Combine(d, "时迁主控台_1.1.1_便携版", "时迁主控台_1.1.0_便携版"));
                candidates.Add(Path.Combine(d, "时迁主控台_1.1.1_便携版"));
                candidates.Add(Path.Combine(d, "时迁主控台_1.1.0_便携版"));
            }

            foreach (string raw in candidates)
            {
                try
                {
                    string full = Path.GetFullPath(raw);
                    if (IsValidPkgRoot(full))
                    {
                        return full;
                    }
                }
                catch
                {
                }
            }
            return null;
        }

        private static bool IsValidPkgRoot(string dir)
        {
            if (string.IsNullOrEmpty(dir) || !Directory.Exists(dir)) return false;
            string py = Path.Combine(dir, "runtime", "python.exe");
            string launcher = Path.Combine(dir, "launcher.py");
            string ui = Path.Combine(dir, "时迁主控台_1.1.0", "console_ui.py");
            return File.Exists(py) && File.Exists(launcher) && File.Exists(ui);
        }

        public bool InitializePaths(out string errorMessage)
        {
            errorMessage = null;
            string exeDir = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\', '/');
            PkgRoot = ResolvePackageRoot(exeDir);
            if (PkgRoot == null)
            {
                LauncherLog.Init(Path.GetTempPath());
                errorMessage = "未找到「时迁主控台 1.1.1 便携版」程序本体。\n\n"
                    + "请把本体目录放在启动器同级，或设置环境变量 SHIQIAN_ROOT 指向本体目录。\n"
                    + "本体目录应包含 runtime\\python.exe 与 时迁主控台_1.1.0\\console_ui.py。";
                LauncherLog.Write("ERROR", "Failed to locate package root from exeDir=" + exeDir);
                return false;
            }

            DirectoryInfo parent = Directory.GetParent(PkgRoot);
            if (parent != null && parent.Name.IndexOf("时迁主控台", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                OuterRoot = parent.FullName;
            }
            else
            {
                OuterRoot = PkgRoot;
            }

            LauncherLog.Init(OuterRoot);
            ProjDir = Path.Combine(PkgRoot, "时迁主控台_1.1.0");
            PythonExe = Path.Combine(PkgRoot, "runtime", "python.exe");
            ConsolePort = ReadConsolePort(Path.Combine(ProjDir, "config.yaml"));

            LauncherLog.Write("INFO", "OuterRoot : " + OuterRoot);
            LauncherLog.Write("INFO", "PkgRoot   : " + PkgRoot);
            LauncherLog.Write("INFO", "PythonExe : " + PythonExe);
            LauncherLog.Write("INFO", "Port      : " + ConsolePort);
            return true;
        }

        private static int ReadConsolePort(string configPath)
        {
            try
            {
                if (File.Exists(configPath))
                {
                    string text = File.ReadAllText(configPath, Encoding.UTF8);
                    Match m = Regex.Match(text, @"consoleport\s*:\s*(\d+)");
                    if (m.Success)
                    {
                        int p;
                        if (int.TryParse(m.Groups[1].Value, out p) && p > 0 && p < 65536)
                        {
                            return p;
                        }
                    }
                }
            }
            catch
            {
            }
            return 5000;
        }

        public bool IsAlreadyServingShiqian()
        {
            return ProbeHttp200(ConsoleUrl) || ProbeHttp200(StatusUrl);
        }

        public bool TryStartBackend(out string errorMessage)
        {
            errorMessage = null;

            if (IsPortInUse(ConsolePort))
            {
                if (IsAlreadyServingShiqian())
                {
                    LauncherLog.Write("INFO", "Existing Shiqian Console detected on port " + ConsolePort + ".");
                    return true;
                }
                errorMessage = "本机端口 127.0.0.1:" + ConsolePort + " 已被其他程序占用。\n\n"
                    + "请关闭占用该端口的程序，或在以下配置文件中修改 server.consoleport：\n"
                    + Path.Combine(ProjDir, "config.yaml") + "\n\n"
                    + "日志文件：\n" + LauncherLog.LogPath;
                LauncherLog.Write("ERROR", "Port conflict on 127.0.0.1:" + ConsolePort);
                return false;
            }

            _jobHandle = NativeJob.CreateKillOnCloseJob();
            try
            {
                var psi = new ProcessStartInfo
                {
                    FileName = PythonExe,
                    Arguments = "-u \"" + Path.Combine(PkgRoot, "launcher.py") + "\"",
                    WorkingDirectory = PkgRoot,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                    StandardOutputEncoding = Encoding.UTF8,
                    StandardErrorEncoding = Encoding.UTF8
                };
                psi.EnvironmentVariables["PYTHONUTF8"] = "1";
                psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
                psi.EnvironmentVariables["PYTHONUNBUFFERED"] = "1";
                psi.EnvironmentVariables["SHIQIAN_NO_BROWSER"] = "1";

                var proc = new Process { StartInfo = psi, EnableRaisingEvents = true };
                proc.OutputDataReceived += (s, e) =>
                {
                    if (!string.IsNullOrEmpty(e.Data)) LauncherLog.Write("Console", e.Data);
                };
                proc.ErrorDataReceived += (s, e) =>
                {
                    if (!string.IsNullOrEmpty(e.Data)) LauncherLog.Write("Console", e.Data);
                };

                if (!proc.Start())
                {
                    throw new InvalidOperationException("无法启动 runtime\\python.exe");
                }
                NativeJob.Attach(_jobHandle, proc);
                proc.BeginOutputReadLine();
                proc.BeginErrorReadLine();

                lock (_lock) { _consoleProcess = proc; }
                LauncherLog.Write("INFO", "Started Shiqian Console backend (PID " + proc.Id + ")");

                var sw = Stopwatch.StartNew();
                while (sw.ElapsedMilliseconds < 20000)
                {
                    if (proc.HasExited)
                    {
                        errorMessage = "时迁主控台后端进程意外退出（退出码 " + proc.ExitCode + "）。\n\n"
                            + "请查看日志文件排查原因：\n" + LauncherLog.LogPath;
                        LauncherLog.Write("ERROR", "Backend exited prematurely with code " + proc.ExitCode);
                        StopAll();
                        return false;
                    }
                    if (ProbeHttp200(ConsoleUrl) || ProbeHttp200(StatusUrl))
                    {
                        LauncherLog.Write("INFO", "Shiqian Console ready in " + sw.ElapsedMilliseconds + "ms at " + ConsoleUrl);
                        return true;
                    }
                    Thread.Sleep(250);
                }

                errorMessage = "等待时迁主控台启动超时（20 秒未响应 " + ConsoleUrl + "）。\n\n"
                    + "日志文件：\n" + LauncherLog.LogPath;
                LauncherLog.Write("ERROR", "Timed out waiting for " + ConsoleUrl);
                StopAll();
                return false;
            }
            catch (Exception ex)
            {
                errorMessage = "启动时迁主控台失败：" + ex.Message + "\n\n日志文件：\n" + LauncherLog.LogPath;
                LauncherLog.Write("ERROR", "Exception starting backend: " + ex);
                StopAll();
                return false;
            }
        }

        private static bool IsPortInUse(int port)
        {
            try
            {
                using (var client = new TcpClient())
                {
                    IAsyncResult ar = client.BeginConnect(IPAddress.Loopback, port, null, null);
                    bool connected = ar.AsyncWaitHandle.WaitOne(200);
                    if (connected && client.Connected)
                    {
                        client.EndConnect(ar);
                        return true;
                    }
                }
            }
            catch
            {
            }
            return false;
        }

        private static bool ProbeHttp200(string url)
        {
            try
            {
                var req = (HttpWebRequest)WebRequest.Create(url);
                req.Method = "GET";
                req.Timeout = 2500;
                req.ReadWriteTimeout = 2500;
                req.Proxy = null;
                using (var resp = (HttpWebResponse)req.GetResponse())
                {
                    return (int)resp.StatusCode == 200;
                }
            }
            catch
            {
                return false;
            }
        }

        private void TryGracefulStopSubmodules()
        {
            try
            {
                var req = (HttpWebRequest)WebRequest.Create(StopApiUrl);
                req.Method = "POST";
                req.ContentLength = 0;
                req.Timeout = 1800;
                req.ReadWriteTimeout = 1800;
                req.Proxy = null;
                using (var resp = (HttpWebResponse)req.GetResponse())
                {
                    LauncherLog.Write("INFO", "Graceful /api/stop returned HTTP " + (int)resp.StatusCode);
                }
            }
            catch
            {
            }
        }

        public void OpenConsoleWindow()
        {
            string browserExe = FindStandaloneAppBrowser();
            if (!string.IsNullOrEmpty(browserExe))
            {
                string profileDir = Path.Combine(OuterRoot, ".browser_profile");
                try { Directory.CreateDirectory(profileDir); } catch { }
                string args = "--app=" + ConsoleUrl
                    + " --proxy-bypass-list=\"127.0.0.1;localhost;192.168.123.*\""
                    + " --user-data-dir=\"" + profileDir + "\""
                    + " --window-size=1280,860"
                    + " --no-first-run --no-default-browser-check";
                LauncherLog.Write("INFO", "Opening standalone app window: " + browserExe + " " + args);
                try
                {
                    Process.Start(new ProcessStartInfo
                    {
                        FileName = browserExe,
                        Arguments = args,
                        UseShellExecute = false
                    });
                    return;
                }
                catch (Exception ex)
                {
                    LauncherLog.Write("WARN", "App window launch failed, falling back to browser: " + ex.Message);
                }
            }
            OpenInDefaultBrowser();
        }

        public void OpenInDefaultBrowser()
        {
            try
            {
                Process.Start(new ProcessStartInfo(ConsoleUrl) { UseShellExecute = true });
            }
            catch (Exception ex)
            {
                LauncherLog.Write("WARN", "Default browser launch failed: " + ex.Message);
            }
        }

        public void OpenPackageFolder()
        {
            try
            {
                if (Directory.Exists(PkgRoot))
                {
                    Process.Start(new ProcessStartInfo("explorer.exe", "\"" + PkgRoot + "\"") { UseShellExecute = true });
                }
            }
            catch (Exception ex)
            {
                LauncherLog.Write("WARN", "Failed to open folder: " + ex.Message);
            }
        }

        public void OpenLogs()
        {
            try
            {
                if (File.Exists(LauncherLog.LogPath))
                {
                    Process.Start(new ProcessStartInfo(LauncherLog.LogPath) { UseShellExecute = true });
                }
            }
            catch (Exception ex)
            {
                LauncherLog.Write("WARN", "Failed to open logs: " + ex.Message);
            }
        }

        private static string FindStandaloneAppBrowser()
        {
            string pf86 = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86);
            string pf = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles);
            string localApp = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            string[] candidates = new[]
            {
                Path.Combine(pf86, "Microsoft", "Edge", "Application", "msedge.exe"),
                Path.Combine(pf, "Microsoft", "Edge", "Application", "msedge.exe"),
                Path.Combine(pf, "Google", "Chrome", "Application", "chrome.exe"),
                Path.Combine(pf86, "Google", "Chrome", "Application", "chrome.exe"),
                Path.Combine(localApp, "Google", "Chrome", "Application", "chrome.exe")
            };
            foreach (string path in candidates)
            {
                try
                {
                    if (!string.IsNullOrEmpty(path) && File.Exists(path)) return path;
                }
                catch
                {
                }
            }
            return null;
        }

        public void StopAll()
        {
            lock (_lock)
            {
                if (_consoleProcess != null || _jobHandle != IntPtr.Zero)
                {
                    LauncherLog.Write("INFO", "Stopping Shiqian Console and child bridge modules...");
                    if (_consoleProcess != null && !_consoleProcess.HasExited)
                    {
                        TryGracefulStopSubmodules();
                    }
                }

                if (_consoleProcess != null)
                {
                    try
                    {
                        if (!_consoleProcess.HasExited)
                        {
                            _consoleProcess.Kill();
                            _consoleProcess.WaitForExit(3000);
                        }
                    }
                    catch { }
                    finally
                    {
                        try { _consoleProcess.Dispose(); } catch { }
                        _consoleProcess = null;
                    }
                }

                if (_jobHandle != IntPtr.Zero)
                {
                    NativeJob.Close(_jobHandle);
                    _jobHandle = IntPtr.Zero;
                }
                LauncherLog.Write("INFO", "All Shiqian Console processes stopped.");
            }
        }

        public void Dispose()
        {
            if (!_disposed)
            {
                _disposed = true;
                StopAll();
            }
        }
    }

    internal sealed class ShiqianControlForm : Form
    {
        public const string StopEventName = "Local\\ShiqianConsole_v111_Stop";
        public const string ShowEventName = "Local\\ShiqianConsole_v111_Show";

        private readonly ConsoleServiceManager _manager;
        private readonly Icon _appIcon;
        private readonly bool _openWindowOnStart;
        private NotifyIcon _trayIcon;
        private EventWaitHandle _stopSignal;
        private RegisteredWaitHandle _stopWait;
        private EventWaitHandle _showSignal;
        private RegisteredWaitHandle _showWait;
        private bool _isExiting;

        public ShiqianControlForm(ConsoleServiceManager manager, bool openWindowOnStart)
        {
            _manager = manager;
            _openWindowOnStart = openWindowOnStart;

            string exePath = Assembly.GetExecutingAssembly().Location;
            _appIcon = Icon.ExtractAssociatedIcon(exePath) ?? SystemIcons.Application;

            Text = "时迁主控台 1.1.1 便携版";
            Icon = _appIcon;
            StartPosition = FormStartPosition.CenterScreen;
            FormBorderStyle = FormBorderStyle.FixedSingle;
            MaximizeBox = false;
            ClientSize = new Size(500, 236);
            BackColor = Color.FromArgb(24, 26, 36);
            ForeColor = Color.FromArgb(238, 240, 246);

            var title = new Label
            {
                Text = "时迁主控台 1.1.1 — 服务运行中",
                Font = new Font("Microsoft YaHei UI", 12f, FontStyle.Bold),
                ForeColor = Color.FromArgb(244, 162, 188),
                Location = new Point(22, 18),
                AutoSize = true
            };

            var info = new Label
            {
                Text = "控制台地址 : " + _manager.ConsoleUrl + "\n"
                     + "程序本体   : " + _manager.OuterRoot + "\n"
                     + "内置解释器 : runtime\\python.exe (免安装便携环境)\n"
                     + "提示说明   : 最小化可隐藏至右下角托盘；点击「停止并退出」将关闭全部桥接进程",
                Font = new Font("Microsoft YaHei UI", 9f),
                ForeColor = Color.FromArgb(204, 212, 226),
                Location = new Point(24, 54),
                Size = new Size(452, 96)
            };

            var btnOpen = CreateActionButton("打开主控台", new Point(22, 168), Color.FromArgb(218, 98, 136), 108);
            btnOpen.Click += (s, e) => _manager.OpenConsoleWindow();

            var btnDir = CreateActionButton("本体目录", new Point(140, 168), Color.FromArgb(58, 72, 98), 104);
            btnDir.Click += (s, e) => _manager.OpenPackageFolder();

            var btnLogs = CreateActionButton("查看日志", new Point(254, 168), Color.FromArgb(58, 72, 98), 104);
            btnLogs.Click += (s, e) => _manager.OpenLogs();

            var btnStop = CreateActionButton("停止并退出", new Point(368, 168), Color.FromArgb(196, 62, 74), 110);
            btnStop.Click += (s, e) => StopAndExit();

            Controls.Add(title);
            Controls.Add(info);
            Controls.Add(btnOpen);
            Controls.Add(btnDir);
            Controls.Add(btnLogs);
            Controls.Add(btnStop);

            Resize += (s, e) =>
            {
                if (WindowState == FormWindowState.Minimized) Hide();
            };

            FormClosing += (s, e) =>
            {
                if (!_isExiting)
                {
                    _isExiting = true;
                    CleanupAndStop();
                }
            };

            Shown += (s, e) =>
            {
                var trayMenu = new ContextMenuStrip();
                trayMenu.Items.Add("打开主控台窗口", null, (ms, me) => _manager.OpenConsoleWindow());
                trayMenu.Items.Add("在默认浏览器打开", null, (ms, me) => _manager.OpenInDefaultBrowser());
                trayMenu.Items.Add("打开本体目录", null, (ms, me) => _manager.OpenPackageFolder());
                trayMenu.Items.Add("查看启动日志", null, (ms, me) => _manager.OpenLogs());
                trayMenu.Items.Add(new ToolStripSeparator());
                trayMenu.Items.Add("显示控制面板", null, (ms, me) => RestoreWindow());
                trayMenu.Items.Add("停止并退出", null, (ms, me) => StopAndExit());

                _trayIcon = new NotifyIcon
                {
                    Icon = _appIcon,
                    Text = "时迁主控台 1.1.1 运行中 (" + _manager.ConsoleUrl + ")",
                    ContextMenuStrip = trayMenu,
                    Visible = true
                };
                _trayIcon.DoubleClick += (ts, te) => _manager.OpenConsoleWindow();

                try
                {
                    _stopSignal = new EventWaitHandle(false, EventResetMode.ManualReset, StopEventName);
                    _stopSignal.Reset();
                    _stopWait = ThreadPool.RegisterWaitForSingleObject(
                        _stopSignal,
                        (state, timedOut) =>
                        {
                            try
                            {
                                if (!IsDisposed && IsHandleCreated) BeginInvoke((Action)StopAndExit);
                            }
                            catch { }
                        },
                        null,
                        -1,
                        true);

                    _showSignal = new EventWaitHandle(false, EventResetMode.AutoReset, ShowEventName);
                    _showWait = ThreadPool.RegisterWaitForSingleObject(
                        _showSignal,
                        (state, timedOut) =>
                        {
                            try
                            {
                                if (!IsDisposed && IsHandleCreated)
                                {
                                    BeginInvoke((Action)(() =>
                                    {
                                        RestoreWindow();
                                        _manager.OpenConsoleWindow();
                                    }));
                                }
                            }
                            catch { }
                        },
                        null,
                        -1,
                        false);
                }
                catch { }

                LauncherLog.Write("INFO", "Control panel and tray icon ready.");
                if (_openWindowOnStart) _manager.OpenConsoleWindow();
            };
        }

        private static Button CreateActionButton(string text, Point location, Color bg, int width)
        {
            var btn = new Button
            {
                Text = text,
                Location = location,
                Size = new Size(width, 38),
                FlatStyle = FlatStyle.Flat,
                BackColor = bg,
                ForeColor = Color.White,
                Font = new Font("Microsoft YaHei UI", 9f, FontStyle.Bold),
                Cursor = Cursors.Hand
            };
            btn.FlatAppearance.BorderSize = 0;
            return btn;
        }

        private void RestoreWindow()
        {
            Show();
            WindowState = FormWindowState.Normal;
            Activate();
        }

        private void StopAndExit()
        {
            if (_isExiting) return;
            _isExiting = true;
            CleanupAndStop();
            Close();
        }

        private void CleanupAndStop()
        {
            try
            {
                if (_stopWait != null) { _stopWait.Unregister(null); _stopWait = null; }
                if (_stopSignal != null) { _stopSignal.Dispose(); _stopSignal = null; }
                if (_showWait != null) { _showWait.Unregister(null); _showWait = null; }
                if (_showSignal != null) { _showSignal.Dispose(); _showSignal = null; }
                if (_trayIcon != null) { _trayIcon.Visible = false; _trayIcon.Dispose(); _trayIcon = null; }
            }
            catch { }
            _manager.StopAll();
        }
    }

    internal static class Program
    {
        [STAThread]
        private static int Main(string[] args)
        {
            try { NativeMethods.SetProcessDPIAware(); } catch { }

            bool noBrowser = string.Equals(
                Environment.GetEnvironmentVariable("SHIQIAN_NO_BROWSER"), "1", StringComparison.Ordinal);
            bool noDialog = string.Equals(
                Environment.GetEnvironmentVariable("SHIQIAN_NO_DIALOG"), "1", StringComparison.Ordinal);

            foreach (string arg in args)
            {
                if (string.Equals(arg, "--version", StringComparison.OrdinalIgnoreCase))
                {
                    return 0;
                }
                if (string.Equals(arg, "--stop", StringComparison.OrdinalIgnoreCase))
                {
                    try
                    {
                        using (EventWaitHandle ev = EventWaitHandle.OpenExisting(ShiqianControlForm.StopEventName))
                        {
                            ev.Set();
                        }
                    }
                    catch { }
                    return 0;
                }
                if (string.Equals(arg, "--no-browser", StringComparison.OrdinalIgnoreCase)) noBrowser = true;
                if (string.Equals(arg, "--no-dialog", StringComparison.OrdinalIgnoreCase)) noDialog = true;
            }

            bool createdNew;
            using (var singleInstanceMutex = new Mutex(true, "Local\\ShiqianConsole_v111_SingleInstance", out createdNew))
            {
                if (!createdNew)
                {
                    // 已在运行：唤醒已有实例并打开主控台窗口
                    try
                    {
                        using (EventWaitHandle showEv = EventWaitHandle.OpenExisting(ShiqianControlForm.ShowEventName))
                        {
                            showEv.Set();
                        }
                    }
                    catch { }
                    return 0;
                }

                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                Application.SetUnhandledExceptionMode(UnhandledExceptionMode.CatchException);

                using (var manager = new ConsoleServiceManager())
                {
                    string initErr;
                    if (!manager.InitializePaths(out initErr))
                    {
                        ShowError(initErr, noDialog);
                        return 1;
                    }

                    Application.ThreadException += (s, e) =>
                    {
                        LauncherLog.Write("FATAL", "UI ThreadException: " + e.Exception);
                        ShowError("时迁主控台启动器界面异常：\n\n" + e.Exception.Message + "\n\n日志：\n" + LauncherLog.LogPath, noDialog);
                    };
                    AppDomain.CurrentDomain.UnhandledException += (s, e) =>
                    {
                        LauncherLog.Write("FATAL", "UnhandledException: " + e.ExceptionObject);
                        ShowError("时迁主控台启动器异常：\n\n" + e.ExceptionObject + "\n\n日志：\n" + LauncherLog.LogPath, noDialog);
                    };
                    AppDomain.CurrentDomain.ProcessExit += (s, e) => manager.StopAll();

                    string startErr;
                    if (!manager.TryStartBackend(out startErr))
                    {
                        ShowError(startErr, noDialog);
                        return 1;
                    }

                    using (var form = new ShiqianControlForm(manager, !noBrowser))
                    {
                        Application.Run(form);
                    }
                }
            }

            LauncherLog.Write("INFO", "Shiqian Console Launcher exited cleanly.");
            return 0;
        }

        private static void ShowError(string message, bool noDialog)
        {
            if (!noDialog)
            {
                try
                {
                    MessageBox.Show(message, "时迁主控台 1.1.1 — 启动提示", MessageBoxButtons.OK, MessageBoxIcon.Error);
                }
                catch { }
            }
        }
    }
}
