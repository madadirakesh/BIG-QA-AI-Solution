using DotNetEnv;
using System.Security.Cryptography;
using System.Text;

namespace SeleniumReqnrollTests.Utils;

/// <summary>
/// Centralised, type-safe access to values from the project's .env file — the C# counterpart of
/// the Java template's ConfigReader.
///
/// The .env file is loaded once (lazily, thread-safely) into the process environment via
/// DotNetEnv. We use TraversePath() so the file is found whether tests run from the project
/// directory or from the bin/ output directory (the .csproj also copies .env to the output, so
/// either way it resolves). Loading is best-effort: if .env is missing the app still runs, and an
/// individual GetProperty() call throws with the missing key — mirroring the Java
/// Dotenv.ignoreIfMissing() behaviour.
/// </summary>
public static class ConfigReader
{
    private static readonly object Lock = new();
    private static bool _loaded;

    private static void EnsureLoaded()
    {
        if (_loaded) return;
        lock (Lock)
        {
            if (_loaded) return;
            try
            {
                // Walk up from the working directory to the nearest .env, then load it.
                Env.TraversePath().Load();
            }
            catch
            {
                // .env is optional at load time; per-key access still validates below.
            }
            _loaded = true;
        }
    }

    /// <summary>Returns the value for <paramref name="key"/>, or throws if missing/blank.</summary>
    public static string GetProperty(string key)
    {
        EnsureLoaded();
        var value = Environment.GetEnvironmentVariable(key);
        if (string.IsNullOrWhiteSpace(value))
        {
            throw new InvalidOperationException($"Property '{key}' not found in .env file");
        }
        // Secrets (e.g. PASSWORD) are stored as "ENC:<token>"; decrypt at run time so callers
        // always get plaintext. Non-encrypted values pass straight through.
        return Decrypt(value);
    }

    /// <summary>Returns the value for <paramref name="key"/>, or <paramref name="fallback"/> if missing/blank.</summary>
    public static string GetOrDefault(string key, string fallback)
    {
        EnsureLoaded();
        var value = Environment.GetEnvironmentVariable(key);
        return string.IsNullOrWhiteSpace(value) ? fallback : Decrypt(value);
    }

    /// <summary>
    /// Decrypt an "ENC:&lt;token&gt;" value produced by the scaffolder (AES-256-GCM). Token layout:
    /// "ENC:" + base64( nonce(12) || ciphertext || gcmTag(16) ); the key is the base64 CRED_KEY in
    /// this project's .env. Values without the ENC: prefix are returned unchanged. Uses the BCL's
    /// AesGcm — no third-party dependency.
    /// </summary>
    private static string Decrypt(string value)
    {
        if (string.IsNullOrEmpty(value) || !value.StartsWith("ENC:"))
        {
            return value;
        }
        var keyB64 = Environment.GetEnvironmentVariable("CRED_KEY");
        if (string.IsNullOrWhiteSpace(keyB64))
        {
            throw new InvalidOperationException("CRED_KEY not found in .env file; cannot decrypt secret");
        }
        var key = Convert.FromBase64String(keyB64);
        var raw = Convert.FromBase64String(value.Substring(4));
        var nonce = raw[..12];
        var tag = raw[^16..];
        var ciphertext = raw[12..^16];
        var plaintext = new byte[ciphertext.Length];
        using var aes = new AesGcm(key, 16);
        aes.Decrypt(nonce, ciphertext, tag, plaintext);
        return Encoding.UTF8.GetString(plaintext);
    }

    /// <summary>Convenience accessor for the most commonly read property.</summary>
    public static string GetAppUrl() => GetProperty("APP_URL");
}
