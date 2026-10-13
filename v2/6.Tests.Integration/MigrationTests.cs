using Npgsql;
using Service.API.Common;

namespace Tests.Integration;

[Collection("api")]
public class MigrationTests
{
    private readonly ApiFixture _api;

    public MigrationTests(ApiFixture api) => _api = api;

    private async Task<List<string>> Column(string sql)
    {
        await using var connection = new NpgsqlConnection(_api.ConnectionString);
        await connection.OpenAsync();
        await using var command = new NpgsqlCommand(sql, connection);
        await using var reader = await command.ExecuteReaderAsync();

        var values = new List<string>();
        while (await reader.ReadAsync())
            values.Add(reader.GetString(0));
        return values;
    }

    [Fact]
    public async Task There_is_one_schema_for_each_context_that_persists_something()
    {
        var schemas = await Column("select schema_name from information_schema.schemata");

        Assert.Contains("catalog", schemas);
        Assert.Contains("community", schemas);
        Assert.Contains("protection", schemas);
        Assert.Contains("census", schemas);
    }

    [Fact]
    public async Task Running_migrate_again_changes_nothing()
    {
        const string history = """select migration_id from "__EFMigrationsHistory" order by 1""";

        var before = await Column(history);

        Assert.Equal(0, await Migrator.RunAsync(_api.Services));

        Assert.NotEmpty(before);
        Assert.Equal(before, await Column(history));
    }
}
