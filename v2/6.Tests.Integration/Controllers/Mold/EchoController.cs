using Application.Common;
using Domain.Shared.Interfaces;
using Domain.Shared.Messaging;
using Domain.Shared.Requests;
using Domain.Shared.Responses;
using FluentValidation;
using Microsoft.AspNetCore.Mvc;
using Service.API.Common;

namespace Tests.Integration.Controllers.Mold;

// Uma rota que só existe na suíte. A pasta é Controllers/Mold, então a convenção tem que
// entregá-la em /api/v2/mold/echo.

public sealed class EchoResponse : QueryResponse
{
    public string? Text { get; set; }
    public string? ServedIn { get; set; }
}

public sealed class EchoRequest : QueryRequest<EchoResponse>
{
    public string? Text { get; set; }
    public int RepeatTimes { get; set; } = 1;
}

public sealed class EchoValidator : AbstractValidator<EchoRequest>
{
    public EchoValidator()
    {
        RuleFor(x => x.Text).NotEmpty().WithMessage("@err.empty_body");
    }
}

public sealed class EchoHandler : QueryHandler<EchoRequest, EchoResponse>
{
    public EchoHandler(IReadRepository repository)
        : base(repository) { }

    public override Task<EchoResponse> Handle(
        EchoRequest request,
        CancellationToken cancellationToken
    )
    {
        if (request.Text == "missing")
        {
            _response.AddError("@err.no_post", 404);
            return Task.FromResult(_response);
        }

        if (request.Text == "boom")
            throw new InvalidOperationException("a handler nobody expected to fail");

        _response.Text = string.Concat(Enumerable.Repeat(request.Text, request.RepeatTimes));
        _response.ServedIn = request.GetLanguage();

        return Task.FromResult(_response);
    }
}

public sealed class ShoutBody
{
    public string? Text { get; set; }
}

[Route("echo")]
public sealed class EchoController : SystemController
{
    public EchoController(IMediator mediator)
        : base(mediator) { }

    [HttpGet]
    public async Task<IActionResult> Get(
        [FromQuery] EchoRequest request,
        CancellationToken cancellationToken
    )
    {
        return GetActionResult(await _mediator.Send(request, cancellationToken));
    }

    [HttpPost]
    public async Task<IActionResult> Post(
        [FromBody] ShoutBody body,
        CancellationToken cancellationToken
    )
    {
        var request = new EchoRequest { Text = body.Text?.ToUpperInvariant() };

        return GetActionResult(await _mediator.Send(request, cancellationToken));
    }
}
