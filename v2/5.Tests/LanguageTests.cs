using Domain.Core.ValueObjects;

namespace Tests;

/// <summary>
/// O mesmo comportamento de <c>Handler.lang</c> na V1: um conjunto fechado, e tudo que está
/// fora dele é inglês.
/// </summary>
public class LanguageTests
{
    [Theory]
    [InlineData("en", "en")]
    [InlineData("pt", "pt")]
    [InlineData("ru", "ru")]
    [InlineData("zh-cn", "zh-cn")]
    [InlineData("zh-tw", "zh-tw")]
    [InlineData(" PT ", "pt")]
    [InlineData("zh_TW", "zh-tw")]
    [InlineData("pt-br", "en")]
    [InlineData("", "en")]
    [InlineData(null, "en")]
    [InlineData("'; drop table posts; --", "en")]
    public void Parse_accepts_the_five_and_falls_back_to_english(string? raw, string code)
    {
        Assert.Equal(code, Language.Parse(raw).Code);
    }

    [Fact]
    public void A_language_nobody_set_is_english_and_equal_to_the_default()
    {
        Assert.Equal("en", default(Language).Code);
        Assert.Equal(Language.Default, default(Language));
        Assert.True(Language.Parse("nope") == Language.Default);
    }
}
