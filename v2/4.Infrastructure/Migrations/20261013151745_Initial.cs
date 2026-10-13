using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace Infrastructure.Migrations
{
    /// <summary>
    /// Um schema por contexto de negócio, antes de existir tabela em qualquer um deles. Perfis
    /// e Embeds não aparecem aqui porque não persistem nada.
    /// </summary>
    public partial class Initial : Migration
    {
        private static readonly string[] Schemas = ["catalog", "community", "protection", "census"];

        /// <inheritdoc />
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            foreach (var schema in Schemas)
                migrationBuilder.EnsureSchema(name: schema);
        }

        /// <inheritdoc />
        protected override void Down(MigrationBuilder migrationBuilder)
        {
            foreach (var schema in Schemas)
                migrationBuilder.DropSchema(name: schema);
        }
    }
}
