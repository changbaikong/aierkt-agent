"""
Agent Core 代码注释指南

这个文件展示了如何为 agent_core 添加高质量的中文注释，
让高中生也能理解代码的工作原理。

## 注释原则

1. **解释为什么，不只是怎么做**
   ❌ 坏例子：# 创建连接
   ✅ 好例子：# 创建连接池，复用连接，避免每次请求都创建新连接（提高性能）

2. **用生活类比帮助理解**
   ❌ 坏例子：# 使用信号量控制并发
   ✅ 好例子：# 就像餐厅只有10张桌子，第11个客人必须等待（信号量控制并发）

3. **标注关键决策的原因**
   ❌ 坏例子：# 设置超时30秒
   ✅ 好例子：# 设置超时30秒（根据95%用户请求在20秒内完成的数据确定）

4. **警告潜在陷阱**
   ❌ 坏例子：# 关闭连接
   ✅ 好例子：# ⚠️ 必须在 finally 块中关闭连接，否则会泄漏资源

5. **提供使用示例**
   每个公共函数/类都应该有 Example 代码块

## 模块级注释模板

```python
\"\"\"
模块名 - 一句话概括功能

详细描述：
- 这个模块做什么
- 为什么需要它
- 主要的类和函数

使用示例：
    from agent_core import SomeClass
    
    obj = SomeClass()
    result = obj.do_something()
\"\"\"
```

## 类注释模板

```python
class MyClass:
    \"\"\"
    类的一句话描述
    
    详细说明：
    - 类的职责
    - 适用场景
    - 与其他类的关系
    
    为什么需要这个类？
    用生活类比解释...
    
    Attributes:
        attr1: 属性1的描述
        attr2: 属性2的描述
    
    Example:
        obj = MyClass(attr1="value")
        result = obj.method()
    \"\"\"
```

## 函数注释模板

```python
def my_function(arg1: str, arg2: int = 10) -> bool:
    \"\"\"
    函数的一句话描述
    
    详细说明函数做什么，为什么这样设计。
    
    Args:
        arg1: 参数1的描述
        arg2: 参数2的描述，默认值10
    
    Returns:
        返回值的描述
    
    Raises:
        ValueError: 什么情况下抛出
        RuntimeError: 什么情况下抛出
    
    为什么设计成这样？
    用生活类比解释...
    
    Example:
        result = my_function("test", 20)
        if result:
            print("成功")
    \"\"\"
```

## 复杂算法注释模板

```python
def complex_algorithm(data: list) -> list:
    \"\"\"
    复杂算法的描述
    
    算法步骤：
    1. 第一步：做什么，为什么
    2. 第二步：做什么，为什么
    3. 第三步：做什么，为什么
    
    时间复杂度：O(n log n)
    空间复杂度：O(n)
    
    为什么选择这个算法？
    - 优点：...
    - 缺点：...
    - 替代方案：...
    
    Example:
        data = [3, 1, 4, 1, 5]
        result = complex_algorithm(data)
        # result = [1, 1, 3, 4, 5]
    \"\"\"
    # 步骤1：初始化（为什么需要初始化）
    result = []
    
    # 步骤2：排序（为什么用这个排序算法）
    sorted_data = sorted(data)  # 使用 Timsort，稳定且高效
    
    # 步骤3：返回（为什么返回新列表而不是修改原列表）
    return sorted_data
```

## 常见模式的注释示例

### 1. 上下文管理器

```python
@contextmanager
def get_resource():
    \"\"\"
    上下文管理器 - 自动管理资源的获取和释放
    
    为什么用 with 语句？
    就像借书，你可能忘记还书，但如果有个自动还书机制（with），
    你离开图书馆时自动还书，就不会忘记了。
    
    Example:
        with get_resource() as res:
            res.do_something()
        # 离开 with 块后，资源自动释放
    \"\"\"
    resource = acquire()
    try:
        yield resource
    finally:
        # 无论是否异常，都释放资源（关键！）
        release(resource)
```

### 2. 装饰器

```python
def my_decorator(func):
    \"\"\"
    装饰器 - 为函数添加额外功能
    
    这个装饰器做什么：
    - 在函数执行前后添加日志
    - 自动捕获异常
    
    为什么用装饰器？
    就像给礼物包装，不改变礼物本身（函数），
    只是在外面加一层包装纸（额外功能）。
    
    Example:
        @my_decorator
        def my_function():
            return "hello"
        
        result = my_function()
        # 自动打印日志："开始执行 my_function"
        # 执行函数
        # 自动打印日志："执行完成"
    \"\"\"
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        print(f"开始执行 {func.__name__}")
        try:
            result = func(*args, **kwargs)
            print(f"执行完成")
            return result
        except Exception as e:
            print(f"执行失败: {e}")
            raise
    return wrapper
```

### 3. 异步函数

```python
async def async_operation():
    \"\"\"
    异步操作 - 不阻塞主线程
    
    什么是异步？
    就像点外卖：
    - 同步：你站在餐厅门口等，外卖到了才能做其他事（阻塞）
    - 异步：你点完外卖继续做其他事，外卖到了手机通知你（不阻塞）
    
    什么时候用异步？
    - I/O 操作（网络请求、文件读写、数据库查询）
    - 不需要立即得到结果的操作
    
    Example:
        result = await async_operation()
        # 在等待期间，程序可以处理其他任务
    \"\"\"
    await asyncio.sleep(1)  # 模拟耗时操作
    return "完成"
```

### 4. 工厂模式

```python
class ObjectFactory:
    \"\"\"
    工厂类 - 统一创建对象的入口
    
    为什么用工厂？
    就像汽车工厂：
    - 不用工厂：你自己买零件、自己组装（复杂、容易出错）
    - 用工厂：告诉工厂你要什么型号，工厂给你造好的车（简单、标准化）
    
    Example:
        factory = ObjectFactory()
        obj = factory.create("type_a")
    \"\"\"
    
    @staticmethod
    def create(obj_type: str):
        \"\"\"
        根据类型创建对象
        
        Args:
            obj_type: 对象类型（"type_a", "type_b"）
        
        Returns:
            创建的对象实例
        
        Raises:
            ValueError: 不支持的类型
        \"\"\"
        if obj_type == "type_a":
            return TypeA()
        elif obj_type == "type_b":
            return TypeB()
        else:
            raise ValueError(f"不支持的类型: {obj_type}")
```

## 需要注释的关键位置

1. ✅ **模块开头**：说明模块的作用
2. ✅ **每个类**：说明类的职责和使用场景
3. ✅ **公共函数/方法**：完整的参数、返回值、异常说明
4. ✅ **复杂的算法/逻辑**：分步骤解释
5. ✅ **魔法数字**：为什么是这个值
6. ✅ **重要的设计决策**：为什么这样设计
7. ✅ **潜在的陷阱**：需要注意的地方
8. ✅ **性能考虑**：时间/空间复杂度

## 不需要注释的地方

1. ❌ 显而易见的代码：`x = 1  # 将1赋值给x`
2. ❌ 变量命名已经说明的：`user_name = get_user_name()  # 获取用户名`
3. ❌ 标准库函数的使用：`sorted(items)  # 排序`

## 注释的语气

- 用"你"而不是"用户"（亲切）
- 用主动语态而不是被动语态（清晰）
- 用简单词汇而不是技术黑话（易懂）
- 用类比而不是抽象概念（具体）

❌ 坏例子：
\"\"\"该模块实现了基于令牌桶算法的速率限制功能\"\"\"

✅ 好例子：
\"\"\"
速率限制模块 - 控制请求频率

就像水龙头限流：
- 桶里有一定数量的令牌（代表你可以发送的请求）
- 每次请求消耗一个令牌
- 令牌以固定速率补充
- 桶满了就不再补充（上限）
\"\"\"
"""
