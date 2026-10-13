namespace Domain.Core.ValueObjects;

/// <summary>
/// O idioma em que o leitor pediu o texto. Conferido contra um conjunto fechado em vez de
/// aceito como veio, porque chega a uma consulta no banco. Quase tudo que a api diz viaja
/// como chave que o navegador traduz; prosa não tem como, alguém precisa ter escrito.
/// </summary>
public readonly struct Language : IEquatable<Language>
{
    private const string Fallback = "en";

    private static readonly HashSet<string> Known = [Fallback, "pt", "ru", "zh-cn", "zh-tw"];

    private readonly string? _code;

    private Language(string code) => _code = code;

    public static Language Default => new(Fallback);

    public static IReadOnlyCollection<string> All => Known;

    public string Code => _code ?? Fallback;

    /// <summary>Qualquer coisa fora do conjunto vira inglês, sem erro.</summary>
    public static Language Parse(string? raw)
    {
        var code = (raw ?? string.Empty).Trim().ToLowerInvariant().Replace('_', '-');
        return Known.Contains(code) ? new Language(code) : Default;
    }

    public bool Equals(Language other) => Code == other.Code;

    public override bool Equals(object? obj) => obj is Language other && Equals(other);

    public override int GetHashCode() => Code.GetHashCode(StringComparison.Ordinal);

    public static bool operator ==(Language left, Language right) => left.Equals(right);

    public static bool operator !=(Language left, Language right) => !left.Equals(right);

    public override string ToString() => Code;
}
