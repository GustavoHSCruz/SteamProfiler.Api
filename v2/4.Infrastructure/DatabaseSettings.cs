using Microsoft.Extensions.Configuration;
using Npgsql;

namespace Infrastructure;

/// <summary>
/// De onde sai o endereço do Postgres. Ou vem inteiro em <c>DB_CONNECTION_STRING</c>, ou é
/// montado das partes, que é o que o compose entrega: a senha é a mesma variável que o
/// container do banco lê, então existe num lugar só.
/// </summary>
public static class DatabaseSettings
{
    public static string ConnectionString(IConfiguration configuration)
    {
        var whole = configuration["DB_CONNECTION_STRING"];
        if (!string.IsNullOrWhiteSpace(whole))
            return whole;

        var builder = new NpgsqlConnectionStringBuilder
        {
            Host = configuration["DB_HOST"] ?? "localhost",
            Port = int.TryParse(configuration["DB_PORT"], out var port) ? port : 5432,
            Database = configuration["DB_NAME"] ?? "steamprofiler",
            Username = configuration["DB_USER"] ?? "steamprofiler",
            Password = configuration["POSTGRES_PASSWORD"],
            // O banco tem teto de conexões baixo de propósito (ver docker-compose.yml), e a
            // api é uma instância só. O pool não pode pedir mais do que ele dá.
            MaxPoolSize = 20,
            // Banco e api conversam por senha, na rede interna do compose. Sem isto o Npgsql
            // tenta negociar criptografia por Kerberos em toda conexão nova, e a imagem de
            // runtime não tem a biblioteca: funciona, mas escreve um erro no log a cada vez.
            GssEncryptionMode = GssEncryptionMode.Disable,
        };

        return builder.ConnectionString;
    }
}
