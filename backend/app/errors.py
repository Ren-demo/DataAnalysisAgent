"""统一业务异常：携带 error_code 与面向用户的中文提示。"""


class AppError(Exception):
    """业务异常基类。

    Attributes:
        message: 面向用户的友好中文提示。
        error_code: 错误码，供前端区分展示：
            - NO_API_KEY       未配置大模型 API Key
            - LLM_UNAVAILABLE  大模型服务不可用（初始化失败 / 网络 / 超时）
            - SQL_ERROR        生成的 SQL 无法执行或多次校验未通过
            - DATASOURCE_UNAVAILABLE  数据源连接失败（外部 MySQL/PostgreSQL 不可达）
            - DATASOURCE_CONNECT_FAILED  新增/编辑连接时测试连接失败（主机/端口/账号/库名不可用）
            - NO_PRIOR_RESULT  重绘图表前当前会话尚无可用查询结果
            - SYSTEM_ERROR     其它未预期异常
    """

    def __init__(self, message: str, error_code: str = "SYSTEM_ERROR"):
        self.message = message
        self.error_code = error_code
        super().__init__(message)
