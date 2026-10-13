using Microsoft.EntityFrameworkCore;
using Microsoft.EntityFrameworkCore.Design;

namespace Infrastructure.Contexts;

/// <summary>
/// Só para o <c>dotnet ef</c>. Gerar uma migration compara o modelo com o snapshot e não abre
/// conexão, então o endereço aqui não precisa existir: assim dá para escrever migration sem
/// banco de pé e sem a api inteira como projeto de partida.
/// </summary>
public class WriteContextFactory : IDesignTimeDbContextFactory<WriteContext>
{
    public WriteContext CreateDbContext(string[] args)
    {
        var options = new DbContextOptionsBuilder<WriteContext>()
            .UseNpgsql(
                "Host=localhost;Database=steamprofiler;Username=steamprofiler",
                DependencyInjection.ConfigureNpgsql
            )
            .UseSnakeCaseNamingConvention()
            .Options;

        return new WriteContext(options);
    }
}
