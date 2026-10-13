namespace Domain.Shared.Entities;

/// <summary>
/// O que toda linha persistida carrega. As duas datas são carimbadas pela unidade de trabalho
/// na hora de salvar, e não por quem cria a entidade: assim o relógio é um só.
/// </summary>
public abstract class Entity
{
    public DateTime CreatedAt { get; private set; }
    public DateTime UpdatedAt { get; private set; }
}

/// <summary>
/// Entidade com a chave que o dado já tem. Não é sempre um Guid: um jogo é o appid dele, um
/// post é a chave do endereço, um recado é um inteiro que já está em URL por aí.
/// </summary>
public abstract class Entity<TId> : Entity
    where TId : notnull
{
    public TId Id { get; protected set; } = default!;
}
