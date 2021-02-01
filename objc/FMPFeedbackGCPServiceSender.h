//
//  FMPFeedbackGCPServiceSender.m
//  FMPFeedbackForm Sender for Google Cloud Platform hosted endpoints
//  Copyright © 2021 Lance Lovette
//  https://github.com/lovette
//
//  Based on FMPZendeskFeedbackSender.m
//  Created by Anton Barkov on 21.01.2020.
//  Copyright © 2020 MacPaw. All rights reserved.
//
//  Licensed under the MIT License.
//  See file LICENSE for full license text

@import Foundation;
#import <FMPFeedbackForm/FMPFeedbackSender.h>

NS_ASSUME_NONNULL_BEGIN

@interface FMPFeedbackGCPServiceSender : NSObject <FMPFeedbackSender>

/// Initializes an instance of our feedback sender object.

/// @param domain The "domain" in "https://domain/fmpfeedback_comment".
/// @param authToken API token used to authenticate request.
/// @param productName Product name, used as a prefix it support ticket's subject, e.g. "[ProductName] Bug Report".
- (instancetype)initWithDomain:(NSString *)domain
                     authToken:(NSString *)authToken
                   productName:(NSString *)productName NS_DESIGNATED_INITIALIZER;

+ (instancetype)new NS_UNAVAILABLE;
- (instancetype)init NS_UNAVAILABLE;

@end

NS_ASSUME_NONNULL_END
