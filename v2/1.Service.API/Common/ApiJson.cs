using System.Text.Json;

namespace Service.API.Common;

public static class ApiJson
{
    /// <summary>
    /// snake_case, como a V1 já responde (<c>updated_at</c>, <c>published_at</c>). O front lê
    /// as duas apis durante a convivência e não deveria precisar de dois dialetos.
    /// </summary>
    public static void Configure(JsonSerializerOptions options)
    {
        options.PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower;
        options.DictionaryKeyPolicy = null;
    }
}
