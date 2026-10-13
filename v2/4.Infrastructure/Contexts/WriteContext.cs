using Microsoft.EntityFrameworkCore;

namespace Infrastructure.Contexts;

/// <summary>
/// O contexto dos comandos: rastreia o que carrega, e é dele que saem as migrations. Cada
/// agregado mora no schema do seu contexto de negócio (catalog, community, protection,
/// census), declarado na configuração da entidade.
/// </summary>
public class WriteContext : DbContext
{
    public WriteContext(DbContextOptions<WriteContext> options)
        : base(options) { }

    protected override void OnModelCreating(ModelBuilder modelBuilder)
    {
        base.OnModelCreating(modelBuilder);

        modelBuilder.ApplyConfigurationsFromAssembly(typeof(WriteContext).Assembly);
    }
}
