using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace YourNamespace
{
    public sealed class LibraryEntryPoint
    {
        private static string DLLDirectory = "";
        private static List<string> DLLDirectoryExpected = new List<string>();
        private static bool isInitialized = false;
        private static readonly object initializationLock = new object();

        public static string libraryDir { get; private set; } = "";

        static LibraryEntryPoint()
        {
            lock (initializationLock)
            {
                if (!isInitialized)
                {
#if DEBUG
                    System.Globalization.CultureInfo.DefaultThreadCurrentUICulture = System.Threading.Thread.CurrentThread.CurrentUICulture = System.Globalization.CultureInfo.InvariantCulture;
                    Assembly thisAssem = typeof(LibraryEntryPoint).Assembly;
                    Console.WriteLine($"Loaded {thisAssem.GetName().Name}");
#endif
                    Assembly assem = typeof(LibraryEntryPoint).Assembly;
                    libraryDir = Path.GetDirectoryName(assem.Location);
                    string libDepFolder = Path.Combine(libraryDir, assem.GetCustomAttribute<AssemblyTitleAttribute>().Title);
                    SetupDllDependencies(libDepFolder);
                }
            }
            isInitialized = true;
        }

        static public void SetupDllDependencies(string dependencyDirPath)
        {
            DLLDirectory = dependencyDirPath;

            if (Directory.Exists(DLLDirectory))
            {
                DLLDirectoryExpected = Directory.GetFiles(DLLDirectory, "*.dll").Select(f => Path.GetFileName(f)).ToList();
            }

            AppDomain.CurrentDomain.ReflectionOnlyAssemblyResolve += ResolveAssembly;
            AppDomain.CurrentDomain.AssemblyResolve += ResolveAssembly;
        }

        static private Assembly ResolveAssembly(Object sender, ResolveEventArgs e)
        {
            Assembly res = null;
            string dllName = $"{e.Name.Split(',')[0]}.dll";
            if (DLLDirectoryExpected.Count > 0 && !DLLDirectoryExpected.Contains(dllName))
            {
                return res;
            }

            string path = Path.Combine(DLLDirectory, dllName);
            try
            {
                res = System.Reflection.Assembly.LoadFile(path);
            }
            catch (Exception ex)
            {
                Console.WriteLine($"Failed to load {path}");
                Console.WriteLine(ex.ToString());
            }
            return res;
        }
    }
}

namespace MusicBeePlugin
{
    using YourNamespace;

    public partial class Plugin
    {
        static private LibraryEntryPoint entryPoint = new LibraryEntryPoint();

        private MusicBeeApiInterface mbApiInterface;
        private PluginInfo about = new PluginInfo();

        // Telemetry tracking state
        private string currentTrackUrl = null;
        private int currentTrackDurationMs = 0;
        private int lastObservedPositionMs = 0;
        private System.Threading.Timer positionTimer = null;

        // Debounce & deduplication controls for manual queue modifications
        private System.Threading.Timer queueDebounceTimer = null;
        private readonly object queueLock = new object();
        private string lastReportedQueueSignature = "";

        // HTTP client for forwarding telemetry to Python bridge server
        private static readonly HttpClient httpClient = new HttpClient()
        {
            Timeout = TimeSpan.FromMilliseconds(1500)
        };
        private const string TELEMETRY_URL = "http://127.0.0.1:5005/event";

        private class QueueSnapshot
        {
            public int TotalTracks;
            public int CurrentIndex;
            public List<string> UpcomingTracks = new List<string>();
            public string Signature => $"{TotalTracks}:{CurrentIndex}:{(UpcomingTracks.Count > 0 ? UpcomingTracks[0] : "")}";
        }

        private class PlayerModeInfo
        {
            public RepeatMode Repeat;
            public bool Shuffle;
            public bool AutoDj;
            public bool Eligible => (Repeat != RepeatMode.One && !AutoDj);
        }

        public PluginInfo Initialise(IntPtr apiInterfacePtr)
        {
            Assembly thisAssem = typeof(Plugin).Assembly;

            string name = thisAssem.GetCustomAttribute<AssemblyTitleAttribute>().Title;
            Version ver = thisAssem.GetName().Version;
            string author = thisAssem.GetCustomAttribute<AssemblyCompanyAttribute>().Company;
            string description = thisAssem.GetCustomAttribute<AssemblyDescriptionAttribute>().Description;

            mbApiInterface = new MusicBeeApiInterface();
            mbApiInterface.Initialise(apiInterfacePtr);

            about.PluginInfoVersion = PluginInfoVersion;
            about.Name = name;
            about.Description = description;
            about.Author = author;
            about.TargetApplication = "";
            about.Type = PluginType.General;
            about.VersionMajor = (short)ver.Major;
            about.VersionMinor = (short)ver.Minor;
            about.Revision = (short)ver.Revision;
            about.MinInterfaceVersion = MinInterfaceVersion;
            about.MinApiRevision = MinApiRevision;
            about.ReceiveNotifications = (ReceiveNotificationFlags.PlayerEvents | ReceiveNotificationFlags.TagEvents);
            about.ConfigurationPanelHeight = 0;

            // Start position tracker timer (runs every 1 second to accurately measure listen time)
            positionTimer = new System.Threading.Timer(TrackPlaybackProgress, null, 1000, 1000);

            // Notify bridge that plugin started and pass process ID for watchdog tracking
            int pid = Process.GetCurrentProcess().Id;
            SendJsonTelemetry(string.Format("{{\"event\":\"PluginStartup\",\"pid\":{0}}}", pid));

            return about;
        }

        private void TrackPlaybackProgress(object state)
        {
            try
            {
                if (mbApiInterface.Player_GetPlayState() == PlayState.Playing)
                {
                    int pos = mbApiInterface.Player_GetPosition();
                    if (pos > 0)
                    {
                        lastObservedPositionMs = pos;
                    }
                }
            }
            catch
            {
                // Never throw on background timer
            }
        }

        public bool Configure(IntPtr panelHandle)
        {
            return false;
        }

        public void SaveSettings()
        {
        }

        public void Close(PluginCloseReason reason)
        {
            try
            {
                // Notify Python server of shutdown immediately
                SendJsonTelemetry("{\"event\":\"MusicBeeClosing\"}");
                Thread.Sleep(50);

                positionTimer?.Dispose();
                positionTimer = null;

                lock (queueLock)
                {
                    queueDebounceTimer?.Dispose();
                    queueDebounceTimer = null;
                }
            }
            catch
            {
            }
        }

        public void Uninstall()
        {
        }

        public void ReceiveNotification(string sourceFileUrl, NotificationType type)
        {
            try
            {
                switch (type)
                {
                    case NotificationType.TrackChanged:
                        HandleTrackChanged();
                        break;

                    case NotificationType.RatingChanged:
                        HandleRatingChanged(sourceFileUrl);
                        break;

                    case NotificationType.PlayingTracksChanged:
                    case NotificationType.PlayingTracksQueueChanged:
                        ScheduleQueueUpdate();
                        break;
                }
            }
            catch (Exception ex)
            {
                mbApiInterface.MB_Trace($"MusicBeeRecommender Error in ReceiveNotification: {ex.Message}");
            }
        }

        private PlayerModeInfo GetPlayerModeInfo()
        {
            var info = new PlayerModeInfo();
            try
            {
                info.Repeat = mbApiInterface.Player_GetRepeat();
                info.Shuffle = mbApiInterface.Player_GetShuffle();
                info.AutoDj = mbApiInterface.Player_GetAutoDjEnabled();
            }
            catch
            {
            }
            return info;
        }

        private QueueSnapshot GetQueueSnapshot()
        {
            var snapshot = new QueueSnapshot();
            try
            {
                string[] files = null;
                bool success = mbApiInterface.NowPlayingList_QueryFilesEx(null, out files);
                snapshot.CurrentIndex = mbApiInterface.NowPlayingList_GetCurrentIndex();
                snapshot.TotalTracks = (success && files != null) ? files.Length : 0;

                if (files != null && snapshot.CurrentIndex >= 0 && snapshot.CurrentIndex < files.Length)
                {
                    for (int i = snapshot.CurrentIndex + 1; i < files.Length && snapshot.UpcomingTracks.Count < 10; i++)
                    {
                        snapshot.UpcomingTracks.Add(files[i]);
                    }
                }
            }
            catch
            {
            }
            return snapshot;
        }

        private void HandleTrackChanged()
        {
            string endedJson = null;

            // 1. Report completion or skip for previous track
            if (!string.IsNullOrEmpty(currentTrackUrl))
            {
                int duration = currentTrackDurationMs;
                int listened = lastObservedPositionMs;
                // Considered skipped if played for < 80% of total duration and not within last 5 seconds
                bool skipped = duration > 0 && listened < (int)(duration * 0.8) && listened < Math.Max(0, duration - 5000);

                endedJson = string.Format(
                    "{{\"event\":\"TrackEnded\",\"track\":{{\"path\":\"{0}\",\"duration_ms\":{1},\"listened_ms\":{2},\"skipped\":{3}}}}}",
                    EscapeJson(currentTrackUrl),
                    duration,
                    listened,
                    skipped ? "true" : "false"
                );
            }

            // 2. Register newly playing track
            currentTrackUrl = mbApiInterface.NowPlaying_GetFileUrl();
            currentTrackDurationMs = mbApiInterface.NowPlaying_GetDuration();
            lastObservedPositionMs = 0;

            string startedJson = null;
            if (!string.IsNullOrEmpty(currentTrackUrl))
            {
                PlayerModeInfo mode = GetPlayerModeInfo();
                QueueSnapshot queue = GetQueueSnapshot();
                lastReportedQueueSignature = queue.Signature; // Mark queue as reported to eliminate duplicate QueueChanged

                startedJson = string.Format(
                    "{{\"event\":\"TrackStarted\",\"track\":{{\"path\":\"{0}\",\"duration_ms\":{1}}},\"player_mode\":{2},\"queue\":{3}}}",
                    EscapeJson(currentTrackUrl),
                    currentTrackDurationMs,
                    FormatPlayerModeJson(mode),
                    FormatQueueJson(queue)
                );
            }

            // Send sequentially: TrackEnded ALWAYS precedes TrackStarted
            Task.Run(async () =>
            {
                if (endedJson != null)
                {
                    await PostJsonAsync(endedJson).ConfigureAwait(false);
                }
                if (startedJson != null)
                {
                    await PostJsonAsync(startedJson).ConfigureAwait(false);
                }
            });
        }

        private void HandleRatingChanged(string sourceFileUrl)
        {
            string targetUrl = string.IsNullOrEmpty(sourceFileUrl) ? mbApiInterface.NowPlaying_GetFileUrl() : sourceFileUrl;
            if (string.IsNullOrEmpty(targetUrl)) return;

            string rating = mbApiInterface.Library_GetFileTag(targetUrl, MetaDataType.Rating);
            string love = mbApiInterface.Library_GetFileTag(targetUrl, MetaDataType.RatingLove);

            string json = string.Format(
                "{{\"event\":\"RatingChanged\",\"path\":\"{0}\",\"rating\":\"{1}\",\"love\":\"{2}\"}}",
                EscapeJson(targetUrl),
                EscapeJson(rating ?? ""),
                EscapeJson(love ?? "")
            );
            SendJsonTelemetry(json);
        }

        private void ScheduleQueueUpdate()
        {
            // Debounce queue notifications by 300ms to swallow rapid multi-event bursts
            lock (queueLock)
            {
                queueDebounceTimer?.Dispose();
                queueDebounceTimer = new System.Threading.Timer(_ => HandleQueueChanged(), null, 300, Timeout.Infinite);
            }
        }

        private void HandleQueueChanged()
        {
            try
            {
                QueueSnapshot queue = GetQueueSnapshot();

                // Deduplicate: avoid sending if queue state hasn't meaningfully changed
                if (queue.Signature == lastReportedQueueSignature)
                {
                    return;
                }
                lastReportedQueueSignature = queue.Signature;

                string json = string.Format(
                    "{{\"event\":\"QueueChanged\",\"total_tracks\":{0},\"current_index\":{1},\"upcoming_tracks\":{2}}}",
                    queue.TotalTracks,
                    queue.CurrentIndex,
                    FormatUpcomingTracksJson(queue.UpcomingTracks)
                );

                SendJsonTelemetry(json);
            }
            catch (Exception ex)
            {
                mbApiInterface.MB_Trace($"MusicBeeRecommender Error in HandleQueueChanged: {ex.Message}");
            }
        }

        private string FormatPlayerModeJson(PlayerModeInfo mode)
        {
            return string.Format(
                "{{\"repeat\":\"{0}\",\"shuffle\":{1},\"auto_dj\":{2},\"eligible\":{3}}}",
                mode.Repeat.ToString(),
                mode.Shuffle ? "true" : "false",
                mode.AutoDj ? "true" : "false",
                mode.Eligible ? "true" : "false"
            );
        }

        private string FormatQueueJson(QueueSnapshot queue)
        {
            return string.Format(
                "{{\"total_tracks\":{0},\"current_index\":{1},\"upcoming_tracks\":{2}}}",
                queue.TotalTracks,
                queue.CurrentIndex,
                FormatUpcomingTracksJson(queue.UpcomingTracks)
            );
        }

        private string FormatUpcomingTracksJson(List<string> tracks)
        {
            StringBuilder sb = new StringBuilder();
            sb.Append("[");
            for (int i = 0; i < tracks.Count; i++)
            {
                if (i > 0) sb.Append(",");
                sb.AppendFormat("\"{0}\"", EscapeJson(tracks[i]));
            }
            sb.Append("]");
            return sb.ToString();
        }

        private async Task PostJsonAsync(string jsonPayload)
        {
            try
            {
                using (StringContent content = new StringContent(jsonPayload, Encoding.UTF8, "application/json"))
                {
                    await httpClient.PostAsync(TELEMETRY_URL, content).ConfigureAwait(false);
                }
            }
            catch
            {
                // Bridge server not running or network timeout - silently fail
            }
        }

        private void SendJsonTelemetry(string jsonPayload)
        {
            // Fire-and-forget asynchronous POST to ensure zero stutter on MusicBee main thread
            Task.Run(async () =>
            {
                await PostJsonAsync(jsonPayload).ConfigureAwait(false);
            });
        }

        private static string EscapeJson(string str)
        {
            if (string.IsNullOrEmpty(str)) return "";
            return str
                .Replace("\\", "\\\\")
                .Replace("\"", "\\\"")
                .Replace("\r", "\\r")
                .Replace("\n", "\\n")
                .Replace("\t", "\\t");
        }
    }
}