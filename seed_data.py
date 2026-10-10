"""数据填充脚本；幂等，已存在的订单号会跳过。"""
import asyncio
import random

from sqlalchemy import select

from config.db import engine, AsyncSessionLocal
from core.security import hash_password
from models import Base
from models.order import Order
from models.product import Product
from models.refund import Refund
from models.user import User
from models.user import ROLE_USER, ROLE_CUSTOMER_SERVICE, ROLE_LEADER, ROLE_ADMIN

PRODUCTS = [
    {"name": "无线蓝牙耳机", "description": "主动降噪，续航 30 小时", "price": 299.00, "stock": 100},
    {"name": "头戴式降噪耳机", "description": "40mm 动圈单元，可折叠收纳", "price": 599.00, "stock": 60},
    {"name": "运动蓝牙耳机", "description": "IPX5 防水，挂耳式设计", "price": 199.00, "stock": 120},
    {"name": "便携蓝牙音箱", "description": "360° 环绕音，IPX7 防水", "price": 259.00, "stock": 80},
    {"name": "智能音箱", "description": "语音助手，可控制智能家居", "price": 349.00, "stock": 45},
    {"name": "20000mAh 充电宝", "description": "双向快充，符合登机标准", "price": 179.00, "stock": 150},
    {"name": "65W 氮化镓充电器", "description": "双口输出，小巧便携", "price": 149.00, "stock": 200},
    {"name": "无线充电器", "description": "15W 快充，兼容多种设备", "price": 129.00, "stock": 150},
    {"name": "USB-C 数据线 2m", "description": "100W 快充，尼龙编织耐磨", "price": 39.00, "stock": 500},
    {"name": "磨砂防摔手机壳", "description": "全包边镜头保护，多色可选", "price": 49.00, "stock": 300},
    {"name": "高清钢化膜 2 片装", "description": "防指纹防刮花", "price": 29.00, "stock": 400},
    {"name": "桌面折叠手机支架", "description": "多角度调节，稳固防滑", "price": 35.00, "stock": 260},
    {"name": "智能摄像头", "description": "2K 高清，支持夜视", "price": 219.00, "stock": 70},
    {"name": "智能手环", "description": "心率监测，14 天续航", "price": 239.00, "stock": 90},
    {"name": "运动相机", "description": "4K 防抖，附防水壳套装", "price": 1299.00, "stock": 25},
    {"name": "入门级航拍无人机", "description": "三轴云台，30 分钟续航", "price": 2499.00, "stock": 8},
    {"name": "直播补光灯", "description": "三色温，桌面夹式安装", "price": 89.00, "stock": 110},
    {"name": "蓝牙自拍杆", "description": "三脚架一体，可拆遥控", "price": 79.00, "stock": 140},
    {"name": "耳机收纳包", "description": "防压防摔，便携硬壳", "price": 25.00, "stock": 320},
    {"name": "车载重力支架", "description": "重力感应锁紧，单手取放", "price": 45.00, "stock": 180},
    {"name": "机械键盘", "description": "87 键热插拔，青轴", "price": 399.00, "stock": 50},
    {"name": "无线键鼠套装", "description": "静音办公，全尺寸布局", "price": 159.00, "stock": 130},
    {"name": "电竞游戏鼠标", "description": "16000DPI 可调，RGB 灯效", "price": 219.00, "stock": 85},
    {"name": "4K 显示器", "description": "27 寸 IPS 面板，支持 HDR", "price": 1299.00, "stock": 30},
    {"name": "2K 曲面显示器", "description": "165Hz 电竞屏，1ms 响应", "price": 1099.00, "stock": 35},
    {"name": "气弹簧显示器支架", "description": "升降旋转，单臂承重 9kg", "price": 189.00, "stock": 75},
    {"name": "铝合金笔记本支架", "description": "折叠便携，散热增高", "price": 89.00, "stock": 160},
    {"name": "USB-C 扩展坞", "description": "10 合 1，支持 4K 输出", "price": 199.00, "stock": 200},
    {"name": "移动固态硬盘 1TB", "description": "1050MB/s 高速传输", "price": 499.00, "stock": 60},
    {"name": "U 盘 128GB", "description": "USB3.2 高速接口", "price": 89.00, "stock": 220},
    {"name": "双频无线网卡", "description": "1300M 高速，免驱安装", "price": 69.00, "stock": 140},
    {"name": "WiFi6 路由器", "description": "双频千兆，穿墙增强", "price": 299.00, "stock": 95},
    {"name": "六类千兆网线 5m", "description": "纯铜线芯，稳定低延迟", "price": 25.00, "stock": 300},
    {"name": "2TB 机械硬盘", "description": "7200 转，大容量存储", "price": 399.00, "stock": 55},
    {"name": "2.5 寸硬盘盒", "description": "Type-C 接口，免工具安装", "price": 79.00, "stock": 110},
    {"name": "家用激光打印机", "description": "黑白打印，无线连接", "price": 899.00, "stock": 40},
    {"name": "4K 无线投屏器", "description": "高清同屏，即插即用", "price": 199.00, "stock": 70},
    {"name": "高清电脑摄像头", "description": "1080P 带麦克风，网课直播", "price": 139.00, "stock": 120},
    {"name": "桌面降噪麦克风", "description": "心形指向，一键静音", "price": 169.00, "stock": 65},
    {"name": "数位手写板", "description": "8192 级压感，附无源笔", "price": 259.00, "stock": 45},
    {"name": "桌面小风扇", "description": "静音三档，USB 供电", "price": 59.00, "stock": 240},
    {"name": "遥控落地扇", "description": "8 档风速，定时开关机", "price": 249.00, "stock": 65},
    {"name": "静音加湿器", "description": "4L 大容量，夜灯模式", "price": 129.00, "stock": 150},
    {"name": "空气炸锅 5L", "description": "可视窗口，不粘易清洁", "price": 399.00, "stock": 80},
    {"name": "双层防烫电热水壶", "description": "1.7L 大容量，自动断电", "price": 99.00, "stock": 200},
    {"name": "大功率吹风机", "description": "速干护发，三档温控", "price": 159.00, "stock": 130},
    {"name": "空气净化器", "description": "除甲醛除菌，智能托管", "price": 899.00, "stock": 40},
    {"name": "智能预约电饭煲", "description": "4L 容量，24 小时预约", "price": 299.00, "stock": 70},
    {"name": "2200W 电磁炉", "description": "大火力爆炒，防水面板", "price": 219.00, "stock": 85},
    {"name": "龙头式净水器", "description": "直饮过滤，安装简单", "price": 599.00, "stock": 35},
    {"name": "激光导航扫地机器人", "description": "扫拖一体，自动回充", "price": 1699.00, "stock": 22},
    {"name": "手持挂烫机", "description": "便携蒸汽，快速除皱", "price": 199.00, "stock": 95},
    {"name": "便携榨汁杯", "description": "充电款，随行鲜榨", "price": 89.00, "stock": 150},
    {"name": "双人双控电热毯", "description": "定时断电，安全防护", "price": 179.00, "stock": 90},
    {"name": "速热静音暖风机", "description": "3 秒速热，倾倒断电", "price": 149.00, "stock": 110},
    {"name": "人体工学椅", "description": "腰部支撑可调，透气网布", "price": 899.00, "stock": 20},
    {"name": "简约办公电脑桌", "description": "1.4m 桌面，带理线孔", "price": 499.00, "stock": 45},
    {"name": "电动升降桌", "description": "记忆高度，久坐神器", "price": 1299.00, "stock": 18},
    {"name": "实木床头柜", "description": "抽屉式收纳，静音滑轨", "price": 259.00, "stock": 55},
    {"name": "五层落地置物架", "description": "承重加固，免打孔", "price": 129.00, "stock": 120},
    {"name": "护眼台灯", "description": "无频闪，多档调光", "price": 159.00, "stock": 140},
    {"name": "智能遥控吸顶灯", "description": "三色变光，记忆亮度", "price": 299.00, "stock": 60},
    {"name": "记忆棉护颈枕头", "description": "慢回弹，贴合颈椎", "price": 129.00, "stock": 160},
    {"name": "全棉四件套", "description": "亲肤透气，多规格可选", "price": 259.00, "stock": 100},
    {"name": "加厚珊瑚绒毛毯", "description": "柔软保暖，可机洗", "price": 99.00, "stock": 180},
    {"name": "加厚可折叠收纳箱 3 只装", "description": "大容量，透明可视", "price": 89.00, "stock": 210},
    {"name": "多格桌面收纳盒", "description": "分类整理，办公好物", "price": 49.00, "stock": 260},
    {"name": "316 不锈钢保温杯", "description": "500ml，12 小时保温", "price": 99.00, "stock": 220},
    {"name": "耐热带盖玻璃水杯", "description": "高硼硅玻璃，防烫设计", "price": 39.00, "stock": 280},
    {"name": "全自动三折雨伞", "description": "晴雨两用，一键开收", "price": 59.00, "stock": 190},
    {"name": "感应开盖垃圾桶", "description": "静音缓降，大容量", "price": 119.00, "stock": 85},
    {"name": "纯棉基础款 T 恤", "description": "圆领百搭，多色可选", "price": 79.00, "stock": 300},
    {"name": "加绒连帽卫衣", "description": "厚实保暖，宽松版型", "price": 159.00, "stock": 150},
    {"name": "三合一冲锋衣", "description": "防风防水，可拆内胆", "price": 499.00, "stock": 60},
    {"name": "速干弹力运动裤", "description": "透气轻便，跑步训练", "price": 129.00, "stock": 140},
    {"name": "缓震透气运动鞋", "description": "轻量回弹，日常跑步", "price": 299.00, "stock": 95},
    {"name": "经典帆布鞋", "description": "百搭耐穿，橡胶大底", "price": 159.00, "stock": 130},
    {"name": "防泼水双肩背包", "description": "独立电脑仓，15.6 寸适配", "price": 229.00, "stock": 105},
    {"name": "20 寸登机行李箱", "description": "万向轮，TSA 密码锁", "price": 399.00, "stock": 48},
    {"name": "遮阳棒球帽", "description": "可调节帽围，透气速干", "price": 59.00, "stock": 240},
    {"name": "纯棉吸汗袜 5 双装", "description": "透气舒适，多色混装", "price": 39.00, "stock": 320},
    {"name": "精品挂耳咖啡 10 片", "description": "中深烘焙，现磨风味", "price": 69.00, "stock": 150},
    {"name": "冷萃拿铁咖啡液 6 杯装", "description": "浓缩液，冷热皆宜", "price": 99.00, "stock": 120},
    {"name": "明前龙井绿茶 100g", "description": "清香回甘，春茶新采", "price": 199.00, "stock": 70},
    {"name": "每日混合坚果 30 包", "description": "独立小袋，健康零食", "price": 129.00, "stock": 160},
    {"name": "72% 黑巧克力", "description": "纯可可脂，微苦回甘", "price": 59.00, "stock": 180},
    {"name": "天然原蜜 500g", "description": "低温灌装，口感纯正", "price": 89.00, "stock": 110},
    {"name": "无糖高纤燕麦片", "description": "即食冲泡，代餐优选", "price": 49.00, "stock": 200},
    {"name": "冻干水果茶组合装", "description": "多种口味，冷水可泡", "price": 79.00, "stock": 140},
    {"name": "TPE 防滑瑜伽垫", "description": "加厚 8mm，附绑带", "price": 89.00, "stock": 130},
    {"name": "包胶哑铃 10kg 一对", "description": "防滚防摔，家用健身", "price": 129.00, "stock": 90},
    {"name": "竞速跳绳", "description": "钢丝轴承，可调绳长", "price": 39.00, "stock": 260},
    {"name": "加厚防爆瑜伽球", "description": "65cm，附打气筒", "price": 69.00, "stock": 110},
    {"name": "速开户外帐篷", "description": "3-4 人，防雨防晒", "price": 399.00, "stock": 42},
    {"name": "超轻伸缩登山杖", "description": "航空铝材，减震设计", "price": 99.00, "stock": 85},
]

USERS = [
    {"username": "demo_user", "password": "demo123", "role": ROLE_USER},
    {"username": "demo_admin", "password": "demo123", "role": ROLE_ADMIN},
    {"username": "demo_leader", "password": "demo123", "role": ROLE_LEADER},
    {"username": "demo_cs", "password": "demo123", "role": ROLE_CUSTOMER_SERVICE},
]

# refund_pending 由退款接口动态产生，不在初始状态池里
ORDER_STATUSES = ["pending", "shipped", "delivered", "refunded"]


async def seed():
    # 生产 schema 由 alembic upgrade head 建，本脚本只灌业务种子
    async with AsyncSessionLocal() as db:
        for u in USERS:
            result = await db.execute(select(User).where(User.username == u["username"]))
            user = result.scalar_one_or_none()
            if user is None:
                db.add(User(username=u["username"], hashed_password=hash_password(u["password"]), role=u["role"]))
                print(f" 用户 {u['username']} 已创建（角色 {u['role']}）")
            elif user.role != u["role"]:
                user.role = u["role"]
                print(f" 用户 {u['username']} 角色已更新为 {u['role']}")

        count = (await db.execute(select(Product))).scalars().all()
        if not count:
            for p in PRODUCTS:
                db.add(Product(**p))
            await db.flush()
            print(f" 商品 {len(PRODUCTS)} 条已插入")
        else:
            print(f" 商品已有 {len(count)} 条，跳过")

        products = {p.name: p for p in (await db.execute(select(Product))).scalars().all()}

        user = (await db.execute(select(User).where(User.username == "demo_user"))).scalar_one()
        existing_orders = (await db.execute(select(Order.order_no))).scalars().all()
        order_count = 8
        for i in range(1, order_count + 1):
            order_no = f"20260816{i:03d}"
            if order_no in existing_orders:
                continue
            product = random.choice(list(products.values()))
            qty = random.randint(1, 3)
            db.add(Order(
                order_no=order_no,
                user_id=user.id,
                product_id=product.id,
                quantity=qty,
                total_price=round(float(product.price) * qty, 2),
                status=random.choice(ORDER_STATUSES),
            ))
        await db.flush()
        orders = (await db.execute(select(Order))).scalars().all()
        print(f" 订单 {len(orders)} 条就绪")

        existing_refunds = (await db.execute(select(Refund.order_id))).scalars().all()
        refund_targets = [o for o in orders if o.status in ("pending", "delivered") and o.id not in existing_refunds][:3]
        for o in refund_targets:
            refund_status = random.choice(["pending", "approved"])
            db.add(Refund(
                order_id=o.id,
                user_id=user.id,
                amount=float(o.total_price),
                reason="不想要了",
                status=refund_status,
                original_status=o.status,
            ))
            # 有进行中退款行的订单，orders.status 必须与之一致，否则撞唯一索引
            o.status = "refunded" if refund_status == "approved" else "refund_pending"
        await db.flush()
        refunds = (await db.execute(select(Refund))).scalars().all()
        print(f" 退款 {len(refunds)} 条就绪")

        print("\n=== 演示数据 ===")
        for o in orders[:3]:
            print(f"订单 {o.order_no} | 商品id={o.product_id} | 金额={o.total_price} | 状态={o.status}")
        for r in refunds[:3]:
            print(f"退款单 {r.id} | 订单 {r.order_id} | 金额={r.amount} | 状态={r.status}")

        await db.commit()
        print("\n 数据填充完成")


if __name__ == "__main__":
    asyncio.run(seed())
