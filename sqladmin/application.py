from typing import TYPE_CHECKING, List, Type, Union

from jinja2 import ChoiceLoader, FileSystemLoader, PackageLoader
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Mount, Route, Router
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

if TYPE_CHECKING:
    from sqladmin.models import ModelAdmin


__all__ = [
    "Admin",
]


class BaseAdmin:
    """Base class for implementing Admin interface.

    Danger:
        This class should almost never be used directly.
    """

    def __init__(
        self,
        app: Starlette,
        engine: Union[Engine, AsyncEngine],
        base_url: str = "/admin",
        title: str = "Admin",
        logo_url: str = None,
    ) -> None:
        self.app = app
        self.engine = engine
        self.base_url = base_url
        self._model_admins: List[Type["ModelAdmin"]] = []

        self.templates = Jinja2Templates("templates")
        self.templates.env.loader = ChoiceLoader(
            [
                FileSystemLoader("templates"),
                PackageLoader("sqladmin", "templates"),
            ]
        )
        self.templates.env.globals["min"] = min
        self.templates.env.globals["admin_title"] = title
        self.templates.env.globals["admin_logo_url"] = logo_url

    @property
    def model_admins(self) -> List[Type["ModelAdmin"]]:
        """Get list of ModelAdmins lazily.

        Returns:
            List of ModelAdmin classes registered in Admin.
        """

        return self._model_admins

    def _find_model_admin(self, identity: str) -> Type["ModelAdmin"]:
        for model_admin in self.model_admins:
            if model_admin.identity == identity:
                return model_admin

        raise HTTPException(status_code=404)

    def _not_found_response(self, request: Request) -> Response:
        context = {"request": request, "status_code": 404, "message": "Not found."}
        return self.templates.TemplateResponse("error.html", context, status_code=404)

    def _unathorized_response(self, request: Request) -> Response:
        context = {"request": request, "status_code": 401, "message": "Unauthorized."}
        return self.templates.TemplateResponse("error.html", context, status_code=401)

    def register_model(self, model: Type["ModelAdmin"]) -> None:
        """Register ModelAdmin to the Admin.

        Args:
            model: ModelAdmin class to register in Admin.

        ???+ usage
            ```python
            from sqladmin import Admin, ModelAdmin

            class UserAdmin(ModelAdmin, model=User):
                pass

            admin.register_model(UserAdmin)
            ```
        """

        # Set database engine from Admin instance
        model.engine = self.engine
        model.sessionmaker = model._get_sessionmaker(model.engine)

        self._model_admins.append(model)


class Admin(BaseAdmin):
    """Main entrypoint to admin interface.

    ???+ usage
        ```python
        from fastapi import FastAPI
        from sqladmin import Admin, ModelAdmin

        from mymodels import User # SQLAlchemy model


        app = FastAPI()
        admin = Admin(app, engine)


        class UserAdmin(ModelAdmin, model=User):
            column_list = [User.id, User.name]


        admin.register_model(UserAdmin)
        ```
    """

    def __init__(
        self,
        app: Starlette,
        engine: Union[Engine, AsyncEngine],
        base_url: str = "/admin",
        title: str = "Admin",
        logo_url: str = None,
    ) -> None:
        """
        Args:
            app: Starlette or FastAPI application.
            engine: SQLAlchemy engine instance.
            base_url: Base URL for Admin interface.
            title: Admin title.
            logo: URL of logo to be displayed instead of title.
        """

        assert isinstance(engine, (Engine, AsyncEngine))
        super().__init__(
            app=app, engine=engine, base_url=base_url, title=title, logo_url=logo_url
        )

        statics = StaticFiles(packages=["sqladmin"])

        router = Router(
            routes=[
                Mount("/statics", app=statics, name="statics"),
                Route("/", endpoint=self.index, name="index"),
                Route("/{identity}/list", endpoint=self.list, name="list"),
                Route("/{identity}/detail/{pk}", endpoint=self.detail, name="detail"),
                Route(
                    "/{identity}/delete/{pk}",
                    endpoint=self.delete,
                    name="delete",
                    methods=["DELETE"],
                ),
                Route(
                    "/{identity}/create",
                    endpoint=self.create,
                    name="create",
                    methods=["GET", "POST"],
                ),
            ]
        )
        self.app.mount(base_url, app=router, name="admin")

        self.templates.env.globals["model_admins"] = self.model_admins

    async def index(self, request: Request) -> Response:
        """Index route which can be overriden to create dashboards."""

        return self.templates.TemplateResponse("index.html", {"request": request})

    async def list(self, request: Request) -> Response:
        """List route to display paginated Model instances."""

        model_admin = self._find_model_admin(request.path_params["identity"])

        page = int(request.query_params.get("page", 1))
        page_size = int(request.query_params.get("page_size", 0))

        pagination = await model_admin.list(page, page_size)

        if pagination.page != 1:
            url = str(request.url.include_query_params(page=page - 1))
            pagination.previous_page_url = url

        if (pagination.page * pagination.page_size) < pagination.count:
            url = str(request.url.include_query_params(page=page + 1))
            pagination.next_page_url = url

        context = {
            "request": request,
            "model_admin": model_admin,
            "pagination": pagination,
        }

        return self.templates.TemplateResponse("list.html", context)

    async def detail(self, request: Request) -> Response:
        """Detail route."""

        model_admin = self._find_model_admin(request.path_params["identity"])
        if not model_admin.can_view_details:
            return self._unathorized_response(request)

        model = await model_admin.get_model_by_pk(request.path_params["pk"])
        if not model:
            return self._not_found_response(request)

        context = {
            "request": request,
            "model_admin": model_admin,
            "model": model,
            "title": model_admin.name,
        }

        return self.templates.TemplateResponse("detail.html", context)

    async def delete(self, request: Request) -> Response:
        """Delete route."""

        identity = request.path_params["identity"]
        model_admin = self._find_model_admin(identity)
        if not model_admin.can_delete:
            return self._unathorized_response(request)

        model = await model_admin.get_model_by_pk(request.path_params["pk"])
        if not model:
            return self._not_found_response(request)

        await model_admin.delete_model(model)

        return Response(content=request.url_for("admin:list", identity=identity))

    async def create(self, request: Request) -> Response:
        """Create model endpoint."""

        identity = request.path_params["identity"]
        model_admin = self._find_model_admin(identity)
        if not model_admin.can_create:
            return self._unathorized_response(request)

        Form = await model_admin.scaffold_form()
        form = Form(await request.form())

        context = {
            "request": request,
            "model_admin": model_admin,
            "form": form,
        }

        if request.method == "GET":
            return self.templates.TemplateResponse("create.html", context)

        if not form.validate():
            return self.templates.TemplateResponse(
                "create.html",
                context,
                status_code=400,
            )

        model = model_admin.model(**form.data)
        await model_admin.insert_model(model)

        return RedirectResponse(
            request.url_for("admin:list", identity=identity),
            status_code=302,
        )
