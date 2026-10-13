using Domain.Shared.Messaging;
using Domain.Shared.Responses;
using Microsoft.AspNetCore.Mvc;

namespace Service.API.Common;

/// <summary>
/// Base dos controllers. Controller não tem regra: recebe o pedido, entrega ao despachante e
/// traduz a resposta em HTTP aqui, num lugar só.
/// </summary>
[ApiController]
public abstract class SystemController : ControllerBase
{
    protected readonly IMediator _mediator;

    protected SystemController(IMediator mediator)
    {
        _mediator = mediator;
    }

    protected ActionResult GetActionResult<TResponse>(TResponse response)
        where TResponse : Response
    {
        if (response.Errors.Count > 0)
        {
            // 400 é o padrão. O handler só troca quando o status faz parte do contrato com
            // quem chama: um post que não existe é 404, e não um pedido malformado.
            return StatusCode(
                response.StatusCode ?? StatusCodes.Status400BadRequest,
                new ErrorResponse(response.Errors.Distinct().ToArray())
            );
        }

        if (response.StatusCode is { } status)
            return StatusCode(status, response);

        if (HttpMethods.IsPost(Request.Method))
            return StatusCode(StatusCodes.Status201Created, response);

        if (HttpMethods.IsDelete(Request.Method))
            return NoContent();

        return Ok(response);
    }
}
