# -*- coding: utf-8 -*-
import hashlib, time, random, decimal, json
from application import app, db
from common.models.book.Book import Book
from common.models.book.BookSaleChangeLog import BookSaleChangeLog
from common.models.pay.PayOrder import PayOrder
from common.models.pay.PayOrderItem import PayOrderItem
from common.models.pay.PayOrderCallbackData import PayOrderCallbackData
from common.libs.Helper import getCurrentDate
from common.libs.queue.QueueService import QueueService
from common.libs.book.BookService import BookService
class PayService():

    def __init__(self):
        pass

    def createOrder(self, member_id, items=None, params=None):
        resp = {'code': 200, 'msg': '操作成功~', 'data': {}}
        # 默认价格为0
        pay_price = decimal.Decimal(0.00)
        continue_cnt = 0
        book_ids = []
        for item in items:
            # 防止价格都小于0
            if decimal.Decimal(item['price']) < 0:
                continue_cnt += 1
                continue

            pay_price = pay_price + decimal.Decimal(item['price']) * int(item['number'])
            book_ids.append(item['id'])

        if continue_cnt >= len(items):
            resp['code'] = -1
            resp['msg'] = '商品items为空~~'
            return resp

        yun_price = params['yun_price'] if params and 'yun_price' in params else 0
        note = params['note'] if params and 'note' in params else ''
        express_address_id = params['express_address_id'] if params and 'express_address_id' in params else 0
        express_info = params['express_info'] if params and 'express_info' in params else {}
        yun_price = decimal.Decimal(yun_price)
        total_price = pay_price + yun_price
        try:
            #为了防止并发库存出问题了，select for update
            tmp_book_list = db.session.query(Book).filter(Book.book_id.in_(book_ids))\
                .with_for_update().all()

            tmp_book_stock_mapping = {}
            for tmp_item in tmp_book_list:
                tmp_book_stock_mapping[tmp_item.book_id] = tmp_item.book_stock

            model_pay_order = PayOrder()
            model_pay_order.order_sn = self.geneOrderSn()
            model_pay_order.member_id = member_id
            model_pay_order.total_price = total_price
            model_pay_order.yun_price = yun_price
            model_pay_order.pay_price = pay_price
            model_pay_order.note = note
            model_pay_order.status = -8
            model_pay_order.express_status = -8
            model_pay_order.express_address_id = express_address_id
            model_pay_order.express_info = json.dumps(express_info)
            model_pay_order.updated_time = model_pay_order.created_time = getCurrentDate()
            db.session.add(model_pay_order)
            #db.session.flush()
            for item in items:
                tmp_left_stock = tmp_book_stock_mapping[item['id']]

                if decimal.Decimal(item['price']) < 0:
                    continue

                if int(item['number']) > int(tmp_left_stock):
                    raise Exception("您购买的这图书太火爆了，剩余：%s,你购买%s~~"%(tmp_left_stock, item['number']))

                tmp_ret = Book.query.filter_by(book_id=item["id"]).update({
                    "book_stock": int(tmp_left_stock) - int(item['number'])
                })

                if not tmp_ret:
                    raise Exception("下单失败请重新下单")

                tmp_pay_item = PayOrderItem()
                tmp_pay_item.pay_order_id = model_pay_order.id
                tmp_pay_item.member_id = member_id
                tmp_pay_item.quantity = item['number']
                tmp_pay_item.price = item['price']
                tmp_pay_item.book_id = item['id']
                tmp_pay_item.note = note
                tmp_pay_item.updated_time = tmp_pay_item.created_time = getCurrentDate()
                db.session.add(tmp_pay_item)
                #db.session.flush()
                print('服务')
                print(item['id'])
                BookService.setStockChangeLog(item['id'], -item['number'], "在线购买")

            db.session.commit()
            resp['data'] = {
                'id': model_pay_order.id,
                'order_sn': model_pay_order.order_sn,
                'total_price': str(total_price)
            }
        except Exception as e:
            db.session.rollback()
            print(e)
            resp['code'] = -1
            resp['msg'] = "下单失败请重新下单"
            resp['msg'] = str(e)
            return resp
        return resp

    def closeOrder(self, pay_order_id=0):
        if pay_order_id < 1:
            return False
        pay_order_info = PayOrder.query.filter_by(id=pay_order_id, status=-8).first()
        if not pay_order_info:
            return False

        pay_order_items = PayOrderItem.query.filter_by(pay_order_id=pay_order_id).all()
        if pay_order_items:
            #需要归还库存
            for item in pay_order_items:
                tmp_book_info = Book.query.filter_by(book_id=item.book_id).first()
                if tmp_book_info:
                    tmp_book_info.book_stock = tmp_book_info.book_stock + item.quantity
                    tmp_book_info.updated_time = getCurrentDate()
                    db.session.add(tmp_book_info)
                    db.session.commit()
                    BookService.setStockChangeLog(item.book_id, item.quantity, "订单取消")

        pay_order_info.status = 0
        pay_order_info.updated_time = getCurrentDate()
        db.session.add(pay_order_info)
        db.session.commit()
        return True

    def orderSuccess(self, pay_order_id=0, params=None):
        try:
            pay_order_info = PayOrder.query.filter_by(id=pay_order_id).first()
            if not pay_order_info or pay_order_info.status not in [-8, -7]:
                return True

            pay_order_info.pay_sn = params['pay_sn'] if params and 'pay_sn' in params else ''
            pay_order_info.status = 1
            pay_order_info.express_status = -7
            pay_order_info.updated_time = getCurrentDate()
            db.session.add(pay_order_info)

            pay_order_items = PayOrderItem.query.filter_by( pay_order_id = pay_order_id ).all()
            for order_item in pay_order_items:
                tmp_model_sale_log = BookSaleChangeLog()
                tmp_model_sale_log.book_id = order_item.book_id
                tmp_model_sale_log.quantity = order_item.quantity
                tmp_model_sale_log.price = order_item.price
                tmp_model_sale_log.member_id = order_item.member_id
                tmp_model_sale_log.created_time = getCurrentDate()
                db.session.add(tmp_model_sale_log )

            db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(e)
            return False

        #加入通知队列，做消息提醒和
        QueueService.addQueue("pay", {
            "member_id": pay_order_info.member_id,
            "pay_order_id": pay_order_info.id
        })
        return True

    def addPayCallbackData(self, pay_order_id=0, type='pay', data=''):
        model_callback = PayOrderCallbackData()
        model_callback.pay_order_id = pay_order_id
        if type == "pay":
            model_callback.pay_data = data
            model_callback.refund_data = ''
        else:
            model_callback.refund_data = data
            model_callback.pay_data = ''

        model_callback.created_time = model_callback.updated_time = getCurrentDate()
        db.session.add(model_callback)
        db.session.commit()
        return True

    def geneOrderSn(self):
        m = hashlib.md5()
        sn = None
        while True:
            str = "%s-%s"%(int(round(time.time() * 1000)), random.randint(0, 9999999))
            m.update(str.encode("utf-8"))
            sn = m.hexdigest()
            if not PayOrder.query.filter_by(order_sn=sn).first():
                break
        return sn
