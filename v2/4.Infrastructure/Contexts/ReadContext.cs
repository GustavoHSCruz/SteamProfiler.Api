using Microsoft.EntityFrameworkCore;

namespace Infrastructure.Contexts;

/// <summary>
/// O contexto das consultas. Mesmo modelo do <see cref="WriteContext"/>, sem rastreamento:
/// consulta projeta e devolve, não guarda o que leu.
/// </summary>
public class ReadContext : DbContext
{
    public ReadContext(DbContextOptions<ReadContext> options)
        : base(options) { }

    protected override void OnModelCreating(ModelBuilder modelBuilder)
    {
        base.OnModelCreating(modelBuilder);

        modelBuilder.ApplyConfigurationsFromAssembly(typeof(ReadContext).Assembly);
    }
}
