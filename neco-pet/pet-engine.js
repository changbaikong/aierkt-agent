// 猫姬桌面宠物引擎
class NecoaPet {
    constructor(containerId, canvasId) {
        this.container = document.getElementById(containerId);
        this.canvas = document.getElementById(canvasId);
        this.ctx = this.canvas.getContext('2d');

        // 动画数据
        this.animations = {};
        this.currentAnimation = null;
        this.currentActionId = null;
        this.currentFrameIndex = 0;
        this.animationFrameId = null;
        this.lastFrameTime = 0;

        // 配置参数
        this.fps = 24;
        this.scale = 0.25;  // 默认25%
        this.imageWidth = 960;
        this.imageHeight = 768;

        // 位置和拖拽
        this.x = window.innerWidth / 2;
        this.y = window.innerHeight / 2;
        this.isDragging = false;
        this.dragOffsetX = 0;
        this.dragOffsetY = 0;

        // 状态系统
        this.state = 'idle';  // idle, walking, action, excited
        this.autoBehaviorEnabled = true;
        this.behaviorFrequency = 3;  // 1-5, 影响随机行为触发频率
        this.lastBehaviorTime = 0;

        // 移动系统
        this.targetX = null;
        this.targetY = null;
        this.moveSpeed = 2;
        this.isMoving = false;

        // 动作名称映射
        this.actionNames = this.initActionNames();

        // 动作分类
        this.actionCategories = this.initActionCategories();

        // 绑定事件
        this.bindEvents();
    }

    // 初始化动作名称
    initActionNames() {
        return {
            '000': '待机',
            '002': '踢脚',
            '030': '点头',
            '100': '反复趴下',
            '190': '持续趴下',
            '191': '趴下抬头看',
            '200': '睁眼踢腿',
            '290': '后空翻',
            '300': '仰头嚣张',
            '340': '大吼',
            '400': '走路',
            '401': '反着走',
            '402': '奔跑',
            '403': '闪现瞬移',
            '404': '飞行',
            '408': '甩尾巴',
            '409': '地上蠕动',
            '500': '跳舞1',
            '501': '趴下摆手',
            '502': '跳舞2',
            '503': '跳舞3',
            '504': '趴着大喊',
            '505': '后飞踢',
            '506': '后仰',
            '507': '惊讶',
            '508': '无语昏晕',
            '600': '晕躺地上',
            '601': '2D站立',
            '606': '旋转',
            '700': '昏倒',
            '705': '躺下',
            '708': '窝着头',
            '806': '连续打技',
            '807': '空中旋转',
            '900': '从地上钻出',
            '901': '摆手跳舞',
            '902': '跳舞4',
            '903': '左右摆跳舞',
            '904': '跳舞5',
            '905': '大喊',
            '910': '拿出武器',
            '911': '炫耀',
            '912': '鞠躬',
            '913': '要美1',
            '930': '要美2',
            '950': '姓冯大喊',
            '951': '放肆大喊',
            '960': '兴奋左右踢腿',
            '980': '兴奋发亮',
            '985': '分解合体',
            '990': '侦探',
            'a000': '待机2',
            'a100': '喝红酒',
            'a101': '摆手无奈',
            'a102': '偷笑'
        };
    }

    // 初始化动作分类
    initActionCategories() {
        return {
            idle: ['000', 'a000', '002', '030', '191', '408'],  // 待机动作
            walk: ['400', '401', '402', '403', '404'],  // 移动动作
            dance: ['500', '501', '502', '503', '901', '902', '903', '904'],  // 跳舞
            excited: ['290', '340', '960', '980', '806', '807'],  // 兴奋/激烈动作
            cute: ['030', '507', '912', '913', '930', 'a101', 'a102'],  // 可爱动作
            tired: ['100', '190', '600', '700', '705', '708'],  // 疲惫动作
            special: ['985', '990', 'a100', '910', '911']  // 特殊动作
        };
    }

    // 获取动作名称
    getActionName(actionId) {
        return this.actionNames[actionId] || `动作${actionId}`;
    }

    // 初始化：加载所有图片
    async init() {
        console.log('开始加载猫姬资源...');
        this.updateInfo('加载中...', '加载资源', '-');

        // 按动作ID分组图片
        const groupedFiles = {};
        IMAGES_LIST.forEach(file => {
            const match = file.match(/(\d+)_(\w+)_(\d+)\.png/);
            if (match) {
                const actionId = match[2];
                const frameNum = parseInt(match[3]);

                if (!groupedFiles[actionId]) {
                    groupedFiles[actionId] = [];
                }
                groupedFiles[actionId].push({ file, frameNum });
            }
        });

        // 对每个动作的帧排序并加载
        const loadPromises = [];
        for (const actionId in groupedFiles) {
            const frames = groupedFiles[actionId].sort((a, b) => a.frameNum - b.frameNum);
            const imagePromises = frames.map(frame => this.loadImage('assets/' + frame.file));
            loadPromises.push(
                Promise.all(imagePromises).then(images => {
                    this.animations[actionId] = images;
                })
            );
        }

        await Promise.all(loadPromises);
        console.log(`成功加载 ${Object.keys(this.animations).length} 个动作！`);

        // 设置初始位置和大小
        this.resetPosition();
        this.updateCanvasSize();

        // 开始播放待机动作
        this.playAction('000');

        // 启动自动行为系统
        this.startAutoBehavior();
    }

    // 加载单张图片
    loadImage(src) {
        return new Promise((resolve, reject) => {
            const img = new Image();
            img.onload = () => resolve(img);
            img.onerror = () => reject(new Error(`加载失败: ${src}`));
            img.src = src;
        });
    }

    // 播放指定动作
    playAction(actionId, loop = false) {
        if (!this.animations[actionId]) {
            console.warn(`动作 ${actionId} 不存在`);
            return;
        }

        // 停止当前动画
        if (this.animationFrameId) {
            cancelAnimationFrame(this.animationFrameId);
        }

        this.currentAnimation = this.animations[actionId];
        this.currentActionId = actionId;
        this.currentFrameIndex = 0;
        this.lastFrameTime = performance.now();
        this.loopAnimation = loop;

        this.updateInfo(this.state, actionId, `(${this.x.toFixed(0)}, ${this.y.toFixed(0)})`);

        // 开始播放
        this.animate();
    }

    // 动画循环
    animate(timestamp = performance.now()) {
        if (!this.currentAnimation || this.currentAnimation.length === 0) return;

        const frameDuration = 1000 / this.fps;

        if (timestamp - this.lastFrameTime >= frameDuration) {
            // 清空画布
            this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);

            // 绘制当前帧
            const img = this.currentAnimation[this.currentFrameIndex];
            this.ctx.drawImage(img, 0, 0, this.canvas.width, this.canvas.height);

            // 下一帧
            this.currentFrameIndex++;
            if (this.currentFrameIndex >= this.currentAnimation.length) {
                if (this.loopAnimation) {
                    this.currentFrameIndex = 0;
                } else {
                    // 动画播放完毕，返回待机状态
                    this.onAnimationComplete();
                    return;
                }
            }

            this.lastFrameTime = timestamp;
        }

        // 如果正在移动，更新位置
        if (this.isMoving && this.targetX !== null && this.targetY !== null) {
            this.updateMovement();
        }

        this.animationFrameId = requestAnimationFrame((ts) => this.animate(ts));
    }

    // 动画播放完成回调
    onAnimationComplete() {
        // 根据当前状态决定下一个动作
        if (this.state === 'idle' || this.state === 'action') {
            this.state = 'idle';
            this.playIdleAnimation();
        } else if (this.state === 'walking') {
            // 如果还在移动中，继续播放走路动画
            if (this.isMoving) {
                this.playAction('400', false);
            } else {
                this.state = 'idle';
                this.playIdleAnimation();
            }
        } else if (this.state === 'excited') {
            // 兴奋状态随机播放兴奋动作
            const excitedActions = this.actionCategories.excited;
            const randomAction = excitedActions[Math.floor(Math.random() * excitedActions.length)];
            this.playAction(randomAction, false);
        }
    }

    // 播放待机动画
    playIdleAnimation() {
        const idleActions = this.actionCategories.idle;
        const randomIdle = idleActions[Math.floor(Math.random() * idleActions.length)];
        this.playAction(randomIdle, false);
    }

    // 更新画布大小
    updateCanvasSize() {
        this.canvas.width = this.imageWidth * this.scale;
        this.canvas.height = this.imageHeight * this.scale;
        this.container.style.width = this.canvas.width + 'px';
        this.container.style.height = this.canvas.height + 'px';
    }

    // 设置缩放
    setScale(scale) {
        this.scale = scale;
        this.updateCanvasSize();
    }

    // 设置FPS
    setFPS(fps) {
        this.fps = parseInt(fps);
    }

    // 设置行为频率
    setBehaviorFrequency(level) {
        this.behaviorFrequency = level;
    }

    // 重置位置
    resetPosition() {
        this.x = window.innerWidth / 2 - (this.imageWidth * this.scale) / 2;
        this.y = window.innerHeight / 2 - (this.imageHeight * this.scale) / 2;
        this.updatePosition();
    }

    // 更新位置
    updatePosition() {
        this.container.style.left = this.x + 'px';
        this.container.style.top = this.y + 'px';
        this.updateInfo(this.state, this.currentActionId, `(${this.x.toFixed(0)}, ${this.y.toFixed(0)})`);
    }

    // 移动到目标位置
    moveTo(targetX, targetY) {
        this.targetX = targetX;
        this.targetY = targetY;
        this.isMoving = true;
        this.state = 'walking';

        // 随机选择移动动画
        const walkActions = this.actionCategories.walk;
        const randomWalk = walkActions[Math.floor(Math.random() * walkActions.length)];
        this.playAction(randomWalk, true);
    }

    // 更新移动
    updateMovement() {
        const dx = this.targetX - this.x;
        const dy = this.targetY - this.y;
        const distance = Math.sqrt(dx * dx + dy * dy);

        if (distance < this.moveSpeed) {
            // 到达目标
            this.x = this.targetX;
            this.y = this.targetY;
            this.isMoving = false;
            this.targetX = null;
            this.targetY = null;
            this.state = 'idle';
            this.playIdleAnimation();
        } else {
            // 继续移动
            this.x += (dx / distance) * this.moveSpeed;
            this.y += (dy / distance) * this.moveSpeed;
        }

        this.updatePosition();
    }

    // 自动行为系统
    startAutoBehavior() {
        setInterval(() => {
            if (!this.autoBehaviorEnabled || this.isDragging) return;
            if (this.state !== 'idle') return;

            const now = Date.now();
            const interval = 10000 / this.behaviorFrequency;  // 频率越高，间隔越短

            if (now - this.lastBehaviorTime < interval) return;

            this.lastBehaviorTime = now;
            this.triggerRandomBehavior();
        }, 1000);
    }

    // 触发随机行为
    triggerRandomBehavior() {
        const rand = Math.random();

        if (rand < 0.3) {
            // 30% 概率：随机移动（允许部分超出屏幕）
            const minVisible = this.canvas.width * 0.2;
            const targetX = Math.random() * (window.innerWidth - minVisible * 2) - this.canvas.width + minVisible;
            const targetY = Math.random() * (window.innerHeight - minVisible * 2) - this.canvas.height + minVisible;
            this.moveTo(targetX, targetY);
        } else if (rand < 0.6) {
            // 30% 概率：播放可爱动作
            const cuteActions = this.actionCategories.cute;
            const randomCute = cuteActions[Math.floor(Math.random() * cuteActions.length)];
            this.state = 'action';
            this.playAction(randomCute, false);
        } else if (rand < 0.8) {
            // 20% 概率：跳舞
            const danceActions = this.actionCategories.dance;
            const randomDance = danceActions[Math.floor(Math.random() * danceActions.length)];
            this.state = 'action';
            this.playAction(randomDance, false);
        } else {
            // 20% 概率：特殊动作
            const specialActions = this.actionCategories.special;
            const randomSpecial = specialActions[Math.floor(Math.random() * specialActions.length)];
            this.state = 'action';
            this.playAction(randomSpecial, false);
        }
    }

    // 切换自动行为
    toggleAutoBehavior() {
        this.autoBehaviorEnabled = !this.autoBehaviorEnabled;
        alert(this.autoBehaviorEnabled ? '自动行为已开启' : '自动行为已关闭');
    }

    // 绑定事件
    bindEvents() {
        // 拖拽事件
        this.container.addEventListener('mousedown', (e) => this.onMouseDown(e));
        document.addEventListener('mousemove', (e) => this.onMouseMove(e));
        document.addEventListener('mouseup', () => this.onMouseUp());

        // 点击事件
        this.container.addEventListener('click', (e) => this.onClick(e));

        // 双击事件
        this.container.addEventListener('dblclick', (e) => this.onDoubleClick(e));

        // 窗口大小变化
        window.addEventListener('resize', () => this.onResize());
    }

    // 鼠标按下
    onMouseDown(e) {
        this.isDragging = true;
        this.dragOffsetX = e.clientX - this.x;
        this.dragOffsetY = e.clientY - this.y;
        this.container.style.cursor = 'grabbing';
        e.preventDefault();
    }

    // 鼠标移动
    onMouseMove(e) {
        if (!this.isDragging) return;

        this.x = e.clientX - this.dragOffsetX;
        this.y = e.clientY - this.dragOffsetY;

        // 允许部分超出屏幕，但至少保留20%可见
        const minVisible = this.canvas.width * 0.2;
        this.x = Math.max(-this.canvas.width + minVisible, Math.min(this.x, window.innerWidth - minVisible));
        this.y = Math.max(-this.canvas.height + minVisible, Math.min(this.y, window.innerHeight - minVisible));

        this.updatePosition();
    }

    // 鼠标松开
    onMouseUp() {
        if (this.isDragging) {
            this.isDragging = false;
            this.container.style.cursor = 'move';
        }
    }

    // 单击
    onClick(e) {
        if (this.isDragging) return;

        // 触发随机交互动作
        const allInteractive = [...this.actionCategories.cute, ...this.actionCategories.dance];
        const randomAction = allInteractive[Math.floor(Math.random() * allInteractive.length)];
        this.state = 'action';
        this.playAction(randomAction, false);
    }

    // 双击
    onDoubleClick(e) {
        // 进入兴奋状态
        this.state = 'excited';
        const excitedActions = this.actionCategories.excited;
        const randomExcited = excitedActions[Math.floor(Math.random() * excitedActions.length)];
        this.playAction(randomExcited, false);

        // 3秒后返回正常
        setTimeout(() => {
            if (this.state === 'excited') {
                this.state = 'idle';
            }
        }, 5000);
    }

    // 窗口大小变化
    onResize() {
        // 允许部分超出屏幕，但至少保留20%可见
        const minVisible = this.canvas.width * 0.2;
        this.x = Math.max(-this.canvas.width + minVisible, Math.min(this.x, window.innerWidth - minVisible));
        this.y = Math.max(-this.canvas.height + minVisible, Math.min(this.y, window.innerHeight - minVisible));
        this.updatePosition();
    }

    // 更新信息显示
    updateInfo(state, action, position) {
        const stateEl = document.getElementById('current-state');
        const actionEl = document.getElementById('current-action');
        const posEl = document.getElementById('position');

        if (stateEl) stateEl.textContent = state;
        if (actionEl) actionEl.textContent = `${action} - ${this.getActionName(action)}`;
        if (posEl) posEl.textContent = position;
    }
}
