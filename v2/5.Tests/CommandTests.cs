using System.Linq.Expressions;
using Application.Common;
using Domain.Shared.Entities;
using Domain.Shared.Events;
using Domain.Shared.Interfaces;
using Domain.Shared.Messaging;
using Domain.Shared.Requests;
using Domain.Shared.Responses;
using Microsoft.Extensions.Logging.Abstractions;

namespace Tests;

public class CommandTests
{
    private sealed class Note : Entity<int>, IAggregateRoot
    {
        public Note(int id, string text)
        {
            Id = id;
            Text = text;
        }

        public string Text { get; }
    }

    private sealed class NoteLeft(int id) : DomainEvent
    {
        public int Id { get; } = id;
    }

    private sealed class LeaveNoteResponse : CommandResponse
    {
        public int NoteId { get; set; }
    }

    private sealed class LeaveNote : CommandRequest<LeaveNoteResponse>
    {
        public string Text { get; set; } = "";
        public bool Refuse { get; set; }
        public bool RefuseLate { get; set; }
        public bool Explode { get; set; }
        public bool ExplodeAfter { get; set; }
    }

    /// <summary>Anota a ordem do que aconteceu, que é o que o molde promete.</summary>
    private sealed class Journal : IUnitOfWork, IMediator
    {
        public List<string> Steps { get; } = [];
        public bool FailOnSave { get; set; }

        public Task<int> SaveChangesAsync(CancellationToken cancellationToken = default)
        {
            if (FailOnSave)
                throw new InvalidOperationException("the database is gone");

            Steps.Add("save");
            return Task.FromResult(1);
        }

        public Task Publish(
            INotification notification,
            CancellationToken cancellationToken = default
        )
        {
            Steps.Add($"publish {notification.GetType().Name}");
            return Task.CompletedTask;
        }

        public Task<TResponse> Send<TResponse>(
            IRequest<TResponse> request,
            CancellationToken cancellationToken = default
        ) => throw new NotSupportedException();
    }

    private sealed class NoRepository : IWriteRepository
    {
        public IQueryable<T> AsQueryable<T>()
            where T : Entity, IAggregateRoot => throw new NotSupportedException();

        public Task<bool> ExistAsync<T>(
            Expression<Func<T, bool>> predicate,
            CancellationToken cancellationToken,
            params Expression<Func<T, object>>[] includes
        )
            where T : Entity, IAggregateRoot => throw new NotSupportedException();

        public Task<T?> SingleAsync<T>(
            Expression<Func<T, bool>> predicate,
            CancellationToken cancellationToken,
            params Expression<Func<T, object>>[] includes
        )
            where T : Entity, IAggregateRoot => throw new NotSupportedException();

        public Task AddAsync<T>(T entity, CancellationToken cancellationToken)
            where T : Entity, IAggregateRoot => throw new NotSupportedException();

        public Task AddRangeAsync<T>(IEnumerable<T> entities, CancellationToken cancellationToken)
            where T : Entity, IAggregateRoot => throw new NotSupportedException();

        public Task DeleteAsync<T>(
            Expression<Func<T, bool>> predicate,
            CancellationToken cancellationToken
        )
            where T : Entity, IAggregateRoot => throw new NotSupportedException();
    }

    private sealed class LeaveNoteCommand(Journal journal)
        : Command<Note, LeaveNote, LeaveNoteResponse, NoteLeft>(
            journal,
            new NoRepository(),
            journal,
            NullLogger.Instance
        )
    {
        protected override Task BeforeChanges(
            LeaveNote request,
            CancellationToken cancellationToken
        )
        {
            journal.Steps.Add("before");

            if (request.Refuse)
                _response.AddError("@err.rejected");

            return Task.CompletedTask;
        }

        protected override Task<Note> Changes(
            LeaveNote request,
            CancellationToken cancellationToken
        )
        {
            journal.Steps.Add("changes");

            if (request.Explode)
                throw new InvalidOperationException("boom");

            if (request.RefuseLate)
                _response.AddError("@err.no_message", 404);

            return Task.FromResult(new Note(7, request.Text));
        }

        protected override Task AfterChanges(Note entity, CancellationToken cancellationToken)
        {
            journal.Steps.Add("after");
            _response.NoteId = entity.Id;
            return Task.CompletedTask;
        }

        protected override NoteLeft? CreateEvent(Note entity) => new(entity.Id);
    }

    private sealed class ExplodesAfterSaving(Journal journal)
        : Command<Note, LeaveNote, LeaveNoteResponse, NoteLeft>(
            journal,
            new NoRepository(),
            journal,
            NullLogger.Instance
        )
    {
        protected override Task BeforeChanges(
            LeaveNote request,
            CancellationToken cancellationToken
        ) => Task.CompletedTask;

        protected override Task<Note> Changes(
            LeaveNote request,
            CancellationToken cancellationToken
        ) => Task.FromResult(new Note(7, request.Text));

        protected override Task AfterChanges(Note entity, CancellationToken cancellationToken) =>
            throw new InvalidOperationException("the listener fell over");
    }

    [Fact]
    public async Task The_four_steps_run_in_order_and_the_event_goes_out_after_the_save()
    {
        var journal = new Journal();

        var response = await new LeaveNoteCommand(journal).Handle(
            new LeaveNote { Text = "hello" },
            CancellationToken.None
        );

        Assert.Equal(["before", "changes", "save", "after", "publish NoteLeft"], journal.Steps);
        Assert.Empty(response.Errors);
        Assert.Equal(7, response.NoteId);
    }

    [Fact]
    public async Task An_error_in_BeforeChanges_stops_before_anything_is_changed()
    {
        var journal = new Journal();

        var response = await new LeaveNoteCommand(journal).Handle(
            new LeaveNote { Refuse = true },
            CancellationToken.None
        );

        Assert.Equal(["before"], journal.Steps);
        Assert.Equal(["@err.rejected"], response.Errors);
    }

    [Fact]
    public async Task An_error_in_Changes_is_not_saved_and_keeps_its_status()
    {
        var journal = new Journal();

        var response = await new LeaveNoteCommand(journal).Handle(
            new LeaveNote { RefuseLate = true },
            CancellationToken.None
        );

        Assert.Equal(["before", "changes"], journal.Steps);
        Assert.Equal(["@err.no_message"], response.Errors);
        Assert.Equal(404, response.StatusCode);
    }

    [Fact]
    public async Task An_exception_before_the_save_becomes_the_key_the_front_already_reads()
    {
        var journal = new Journal();

        var response = await new LeaveNoteCommand(journal).Handle(
            new LeaveNote { Explode = true },
            CancellationToken.None
        );

        Assert.DoesNotContain("save", journal.Steps);
        Assert.Equal(["@err.save_failed"], response.Errors);
        Assert.Equal(500, response.StatusCode);
    }

    [Fact]
    public async Task A_save_that_fails_publishes_nothing()
    {
        var journal = new Journal { FailOnSave = true };

        var response = await new LeaveNoteCommand(journal).Handle(
            new LeaveNote(),
            CancellationToken.None
        );

        Assert.Equal(["before", "changes"], journal.Steps);
        Assert.Equal(["@err.save_failed"], response.Errors);
    }

    [Fact]
    public async Task What_fails_after_the_save_does_not_turn_a_saved_write_into_an_error()
    {
        var journal = new Journal();

        var response = await new ExplodesAfterSaving(journal).Handle(
            new LeaveNote(),
            CancellationToken.None
        );

        Assert.Equal(["save"], journal.Steps);
        Assert.Empty(response.Errors);
    }

    [Fact]
    public async Task A_caller_that_gave_up_is_not_answered_with_a_save_error()
    {
        using var cancelled = new CancellationTokenSource();
        await cancelled.CancelAsync();

        await Assert.ThrowsAnyAsync<OperationCanceledException>(() =>
            new GivesUp(new Journal()).Handle(new LeaveNote(), cancelled.Token)
        );
    }

    private sealed class GivesUp(Journal journal)
        : Command<Note, LeaveNote, LeaveNoteResponse, NoteLeft>(
            journal,
            new NoRepository(),
            journal,
            NullLogger.Instance
        )
    {
        protected override Task BeforeChanges(
            LeaveNote request,
            CancellationToken cancellationToken
        )
        {
            cancellationToken.ThrowIfCancellationRequested();
            return Task.CompletedTask;
        }

        protected override Task<Note> Changes(
            LeaveNote request,
            CancellationToken cancellationToken
        ) => throw new NotSupportedException();
    }
}
