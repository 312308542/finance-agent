from finance_agent.storage.repositories import ModelRuntimeConfigRepository


class FakeResult:
    def __init__(self, rows=None, scalar_value=None):
        self.rows = rows or []
        self.scalar_value = scalar_value

    def scalars(self):
        return self

    def one(self):
        if not self.rows:
            raise LookupError("missing row")
        return self.rows[0]

    def one_or_none(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)

    def scalar(self):
        return self.scalar_value


class FakeSession:
    def __init__(self, model, routes):
        self.model = model
        self.routes = routes
        self.executed = []
        self.flushed = False

    def scalars(self, statement):
        text = str(statement)
        if "model_instances" in text:
            return FakeResult([self.model])
        if "model_routing_rules" in text:
            return FakeResult(self.routes)
        return FakeResult([])

    def execute(self, statement):
        self.executed.append(str(statement))
        return FakeResult()

    def flush(self):
        self.flushed = True


class FakeModel:
    model_key = "qwen-plus"
    is_enabled = True


class FakeRoute:
    is_enabled = True


def test_disable_model_instance_disables_referencing_routes() -> None:
    """删除模型实例时停用模型和指向它的路由，保留历史记录。"""

    model = FakeModel()
    routes = [FakeRoute(), FakeRoute()]
    session = FakeSession(model=model, routes=routes)

    deleted = ModelRuntimeConfigRepository(session).disable_model_instance("qwen-plus")

    assert deleted is model
    assert model.is_enabled is False
    assert [route.is_enabled for route in routes] == [False, False]
    assert session.flushed is True
